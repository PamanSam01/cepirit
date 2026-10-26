"""
Cepirit Trading Engine
======================
Automated prediction market execution and eligibility engine.
"""

import os
import asyncio
import argparse
import logging
import random
import re
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple

from playwright.async_api import async_playwright, Browser, Page, Playwright, BrowserContext
from eligibility import EligibilityTracker, calculate_safe_bet_amount, select_natural_candidates

# --- Config ---

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

_DEFAULT_HOST = "".join(["ace", "pyr.com"])
BASE_URL = os.getenv("TARGET_URL", f"https://www.{_DEFAULT_HOST}")

# Market fallback (jika tidak bisa scrape dari /live)
# Format: (category, asset_slug, duration_minutes)
MARKETS_FALLBACK = [
    ("crypto", "btc",  60),
    ("crypto", "eth",  60),
    ("crypto", "sol",  60),
    ("crypto", "xrp",  60),
    ("crypto", "bnb",  60),
    ("crypto", "hype", 60),
    ("stock",  "nvda", 60),
    ("stock",  "spcx", 60),
    ("stock",  "pltr", 360),
    ("stock",  "meta", 360),
    ("stock",  "tsla", 360),
    ("stock",  "spy",  360),
]

MIN_TRADE    = 1    # minimal trade untuk streak/multi-market
PROFIT_TRADE = 5   # untuk profit position tasks
WAIT_BETWEEN = 3   # detik antar market
WAIT_LOAD    = 5   # detik tunggu page load

# --- Helpers ---

def get_round_timestamps() -> List[str]:
    """
    Generate timestamp untuk round-round yang mungkin aktif.
    Acepyr market round dimulai setiap jam (menit 00).
    Timestamp menggunakan UTC time.
    Cek jam ini, jam berikutnya, dan jam sebelumnya.
    """
    now = datetime.now(timezone.utc)
    timestamps = []
    for delta in range(-1, 3):
        t = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=delta)
        timestamps.append(t.strftime("%Y%m%d%H%M"))
    return timestamps

async def discover_live_markets(page) -> List[str]:
    """
    Scrape halaman /live untuk mendapatkan URL market yang aktif sekarang.
    Return list URL market yang aktif.
    """
    log.info("Mencari market aktif dari /live...")
    await page.goto(f"{BASE_URL}/live", wait_until="domcontentloaded")
    await asyncio.sleep(WAIT_LOAD)
    
    # Cari semua href yang berformat /live/...
    market_urls = await page.evaluate("""
        () => {
            const seen = new Set();
            const result = [];
            document.querySelectorAll('a[href*="/live/"]').forEach(a => {
                const href = a.href;
                if (!seen.has(href) && href.includes('up-or-down')) {
                    seen.add(href);
                    result.push(href);
                }
            });
            return result;
        }
    """)
    
    # Juga cek dari button clicks yang mungkin mengandung link
    if not market_urls:
        log.warning("Tidak ada link /live/ ditemukan. Coba scroll halaman...")
        await page.keyboard.press("End")
        await asyncio.sleep(2)
        market_urls = await page.evaluate("""
            () => {
                const seen = new Set();
                const result = [];
                document.querySelectorAll('[href*="/live/"], [data-href*="/live/"]').forEach(el => {
                    const href = el.href || el.dataset.href;
                    if (href && !seen.has(href) && href.includes('up-or-down')) {
                        seen.add(href);
                        result.push(href);
                    }
                });
                return result;
            }
        """)
    
    if market_urls:
        log.info(f"Ditemukan {len(market_urls)} market aktif")
        for u in market_urls[:5]:
            log.info(f"  {u}")
    else:
        log.warning("Tidak bisa scrape market URLs, pakai fallback timestamp")
        # Fallback: generate dari daftar market yang diketahui hanya untuk jam ini
        ts_now = get_round_timestamps()[1] # index 1 = current hour
        for cat, asset, dur in MARKETS_FALLBACK:
            market_urls.append(f"{BASE_URL}/live/{cat}-{asset}-up-or-down-{dur}min-{ts_now}")
    
    return market_urls

def build_url(category: str, asset: str, duration: int, ts: Optional[str] = None) -> str:
    if ts is None:
        ts = get_round_timestamps()[1]  # jam sekarang
    return f"{BASE_URL}/live/{category}-{asset}-up-or-down-{duration}min-{ts}"

def mname(category: str, asset: str, duration: int) -> str:
    d = f"{duration // 60}HR" if duration >= 60 else f"{duration}MIN"
    return f"{asset.upper()} UP OR DOWN {d}"

# --- Page Actions ---

async def get_balance(page: Page) -> Optional[float]:
    try:
        # Panggil API internal untuk mengambil balance secara akurat
        bal = await page.evaluate('''async () => {
            try {
                const res = await fetch("/api/economy/balance");
                const json = await res.json();
                if (json && json.data && json.data.balance !== undefined && json.data.balance !== null) return json.data.balance;
                if (json && json.balance !== undefined && json.balance !== null) return json.balance;
                return null;
            } catch(e) { return null; }
        }''')
        if bal is not None:
            return float(bal)
        return None
    except Exception as e:
        log.warning(f"Gagal memanggil API balance: {e}")
        return None

async def nav_market(page: Page, url: str, name: str) -> bool:
    """Navigasi ke market. Return True jika market masih terbuka (bisa trade)."""
    log.info(f"  -> Navigating to: {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(WAIT_LOAD)
        
        # Cek apakah ada panel sama sekali (tunggu hingga 12 detik, retry 1x reload)
        panel = page.locator(".otrade-panel").first
        try:
            await panel.wait_for(timeout=12000)
        except Exception:
            log.warning("  Panel tidak muncul, coba reload halaman...")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(WAIT_LOAD + 2)
                await panel.wait_for(timeout=10000)
                log.info("  Panel muncul setelah reload.")
            except Exception:
                log.warning("  Panel tidak ditemukan setelah reload - market mungkin sedang closing/resolving")
                return False
        
        # Cek apakah market masih bisa di-trade (bukan resolving/resolved)
        panel_class = await panel.get_attribute("class") or ""
        if "otrade-outcome" in panel_class:
            title_el = page.locator(".otrade-outcome__title").first
            try:
                title = await title_el.inner_text(timeout=2000)
                log.warning(f"  Market sudah closed: {title.strip()}")
            except Exception:
                log.warning("  Market sudah closed (otrade-outcome state)")
            return False
        
        return True
    except Exception as e:
        log.warning(f"  Market tidak tersedia: {e}")
        return False

async def select_outcome(page: Page, outcome: str = "up") -> bool:
    """
    Pilih Up atau Down outcome secara akurat.
    Hanya mengklik jika tombol outcome saat ini belum aktif (mencegah unselect / toggle-off).
    """
    try:
        target = outcome.strip().lower() # 'up' atau 'down'
        buttons = await page.locator(".otrade-panel button.button-3d").all()
        for b in buttons:
            txt = (await b.inner_text()).strip().lower()
            if target in txt:
                cls = await b.get_attribute("class") or ""
                # Hanya klik jika status tombol masih unselected
                if "button-3d--unselected" in cls:
                    await b.click()
                    await asyncio.sleep(0.5)
                    log.info(f"  [Outcome] Klik pilih: {outcome.upper()}")
                else:
                    log.info(f"  [Outcome] {outcome.upper()} sudah aktif (skip toggle)")
                return True
                
        # Fallback jika class bukan button-3d
        for sel in [f".otrade-panel button:has-text('{outcome.capitalize()}')", f"button[data-side='{target}']"]:
            b = page.locator(sel).first
            if await b.is_visible(timeout=2000):
                cls = await b.get_attribute("class") or ""
                if "button-3d--unselected" in cls or ("positive" not in cls and "negative" not in cls and "selected" not in cls):
                    await b.click()
                    await asyncio.sleep(0.5)
                log.info(f"  [Outcome Fallback] Outcome: {outcome.upper()}")
                return True

        log.warning(f"  Tombol outcome {outcome} tidak ditemukan")
        return False
    except Exception as e:
        log.warning(f"  Gagal select outcome {outcome}: {e}")
        return False

async def set_amount(page: Page, amount: float):
    """Set jumlah trade ACEPYR menggunakan input field langsung (prioritas) dan fallback chips."""
    try:
        amt_int = int(amount)
        if amt_int <= 0:
            raise Exception("Amount harus > 0")
        
        # 1. Prioritas Utama: Langsung isi input field (bebas bug chip React number-flow)
        inp = page.locator("input.otrade-panel__amount-input, input[aria-label='Amount in ACEPYR']").first
        if await inp.is_visible(timeout=3000):
            await inp.click()
            await inp.fill(str(amt_int))
            await inp.dispatch_event("input")
            await inp.dispatch_event("change")
            await asyncio.sleep(0.5)
            log.info(f"  Amount set via input: {amt_int} ACEPYR")
            return
            
        # 2. Fallback: Chip buttons jika input field tidak terdeteksi
        all_chips = await page.locator('.otrade-panel__chip').all()
        visible_chips = [c for c in all_chips if await c.is_visible()]
        if len(visible_chips) >= 4:
            chip_values = [(visible_chips[3], 100), (visible_chips[2], 10), 
                           (visible_chips[1], 5), (visible_chips[0], 1)]
            remaining = amt_int
            for chip, value in chip_values:
                clicks = remaining // value
                for _ in range(clicks):
                    await chip.click()
                    await asyncio.sleep(0.05)
                remaining %= value
            await asyncio.sleep(0.5)
            log.info(f"  Amount set via chips: {amt_int} ACEPYR")
    except Exception as e:
        log.warning(f"  Set amount warning: {e}")

async def click_trade(page: Page, dry_run: bool = False, account_name: str = "main") -> bool:
    """Klik tombol Trade dan verifikasi hasilnya dengan network logging & screenshot."""
    try:
        btn = page.locator("button.otrade-panel__submit").first
        if not await btn.is_visible(timeout=6000):
            log.warning("  Trade button tidak visible")
            return False
            
        btn_text = (await btn.inner_text()).strip()
        log.info(f"  Submit button text: '{btn_text}'")
        
        if "select" in btn_text.lower():
            log.warning(f"  Outcome belum terpilih dengan benar (text: '{btn_text}')")
            return False
            
        if await btn.is_disabled():
            log.warning(f"  Trade button disabled (text: '{btn_text}')")
            return False
        
        if dry_run:
            log.info("  [DRY RUN] Skip klik Trade")
            return True
        
        bal_before = await get_balance(page)
        
        # Pasang listener untuk menangkap request dan response API saat tombol diklik
        req_count = [0]
        order_api_success = [False]
        def req_handler(r):
            if any(k in r.url for k in ["api", "order", "trade", "positions", "economy"]):
                req_count[0] += 1
                log.info(f"    -> [NET API REQ] {r.method} {r.url}")
                
        def resp_handler(r):
            if any(k in r.url for k in ["api", "order", "trade", "positions", "economy"]):
                log.info(f"    <- [NET API RESP] {r.status} {r.url}")
                if "book/orders" in r.url and r.status in [200, 201]:
                    order_api_success[0] = True

        page.on("request", req_handler)
        page.on("response", resp_handler)
            
        # Klik tombol submit dengan retry jika order book masih loading
        for attempt in range(4):
            await btn.click(timeout=5000)
            log.info(f"  Tombol '{btn_text}' diklik (attempt {attempt+1}/4). Menunggu reaksi network...")
            await asyncio.sleep(2)
            
            # Cek jika ada notifikasi loading atau error
            toast = await page.evaluate('''() => {
                const el = document.querySelector('.toast, .notification, [role="alert"], .otrade-panel__error, .error');
                return el ? el.innerText.trim() : null;
            }''')
            if toast:
                log.info(f"  Pesan di layar browser: '{toast}'")
                if "loading" in toast.lower():
                    log.warning("  Order book masih loading, tunggu 3 detik sebelum retry...")
                    await asyncio.sleep(3)
                    continue
            break
        
        # Simpan screenshot layar setelah klik untuk diagnosa
        try:
            scr_path = f"trade_click_{account_name}.png"
            await page.screenshot(path=scr_path)
            log.info(f"  Screenshot setelah klik disimpan: {scr_path}")
        except Exception:
            pass

        # Bersihkan toast lama agar layar bersih
        try:
            await page.evaluate('''() => {
                document.querySelectorAll('.toast, [role="alert"]').forEach(el => el.remove());
            }''')
        except Exception:
            pass

        # Handle konfirmasi modal jika ada
        for conf_sel in [
            "button:has-text('Confirm')",
            "button:has-text('Place Trade')",
            "button:has-text('Place Order')",
            ".modal button.button-3d--positive",
            ".modal button.otrade-panel__submit"
        ]:
            try:
                conf = page.locator(conf_sel).first
                if await conf.is_visible(timeout=1000):
                    c_txt = (await conf.inner_text()).strip()
                    log.info(f"  Modal konfirmasi terdeteksi: '{c_txt}', klik konfirmasi...")
                    await conf.click()
                    await asyncio.sleep(2)
                    break
            except Exception:
                pass
        
        # Lepas listener
        try:
            page.remove_listener("request", req_handler)
            page.remove_listener("response", resp_handler)
        except Exception:
            pass
        
        # Verifikasi: polling balance atau konfirmasi order book API
        for attempt in range(6):
            if order_api_success[0]:
                log.info(f"  ✅ [ORDER PLACED] Order berhasil dikonfirmasi oleh server (API 200/201 OK)")
                return True
            await asyncio.sleep(2)
            bal_after = await get_balance(page)
            if bal_before and bal_after and bal_after < bal_before:
                log.info(f"  ✅ [TRADE CONFIRMED] Balance: {bal_before:.2f} -> {bal_after:.2f} (-{bal_before - bal_after:.2f})")
                return True
            if order_api_success[0]:
                log.info(f"  ✅ [ORDER PLACED] Order berhasil dikonfirmasi oleh server (API 200/201 OK)")
                return True
                
        # Cek pesan akhir
        try:
            toast = await page.evaluate('''() => {
                const el = document.querySelector('.toast, .notification, [role="alert"], .otrade-panel__error, .error');
                return el ? el.innerText.trim() : null;
            }''')
            if toast:
                log.warning(f"  Pesan di layar browser: '{toast}'")
        except Exception:
            pass
            
        log.warning(f"  Trade mungkin gagal - balance tidak berubah setelah 12s: {bal_before} -> {bal_after} (API requests: {req_count[0]})")
        return False
    except Exception as e:
        log.error(f"  Trade button error: {e}")
        return False

async def place_order(page, url, name, outcome="up", amount=MIN_TRADE, dry_run=False, account_name="main") -> bool:
    if not await nav_market(page, url, name):
        return False
    try:
        # 1. Tunggu harga live aktif (tombol Up/Down tidak mengandung '--')
        log.info("  Menunggu harga live aktif...")
        for _ in range(20):
            up_btn = page.locator(".otrade-panel button:has-text('Up')").first
            down_btn = page.locator(".otrade-panel button:has-text('Down')").first
            up_txt = (await up_btn.inner_text()).strip() if await up_btn.is_visible() else ""
            down_txt = (await down_btn.inner_text()).strip() if await down_btn.is_visible() else ""
            if up_txt and "--" not in up_txt and down_txt and "--" not in down_txt:
                break
            await asyncio.sleep(1)

        # 2. Pastikan Buy mode aktif
        buy_btn = page.locator(".otrade-panel__ptype-btn:has-text('Buy')").first
        if await buy_btn.is_visible(timeout=2000):
            cls = await buy_btn.get_attribute("class") or ""
            if "otrade-panel__ptype-btn--on" not in cls:
                await buy_btn.click()
                await asyncio.sleep(0.5)

        # 3. Pilih Outcome
        await select_outcome(page, outcome)
        await asyncio.sleep(0.5)
        
        # 4. Set Amount via direct input
        await set_amount(page, amount)
        
        # 5. Tunggu kalkulasi order book (To Win / Avg. Price muncul)
        await asyncio.sleep(2.0)
        
        return await click_trade(page, dry_run, account_name=account_name)
    except Exception as e:
        log.error(f"  Error place order: {e}")
        return False

async def check_user_activity(page: Page, account_name: str = "main"):
    """Cek menu / tab Activity & Trades di halaman profile Acepyr."""
    log.info(f"[{account_name}] === MEMERIKSA MENU / TAB ACTIVITY & TRADES ===")
    try:
        # Buka home dulu untuk membaca link profile user
        await page.goto(f"{BASE_URL}/", wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(2)
        
        # Cari link profile (@username) dari header atau sidebar
        profile_path = await page.evaluate('''() => {
            const a = document.querySelector('a[href*="/@"]');
            return a ? a.getAttribute('href') : null;
        }''')
        
        if not profile_path:
            # Fallback cari avatar icon
            profile_path = await page.evaluate('''() => {
                const links = Array.from(document.querySelectorAll('a'));
                const p = links.find(l => l.href.includes('/@'));
                return p ? p.getAttribute('href') : null;
            }''')

        if profile_path:
            target_url = profile_path if profile_path.startswith("http") else f"{BASE_URL}{profile_path}"
            log.info(f"[{account_name}] Buka Profile: {target_url}...")
            await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
            
            # Cek tab Trades
            trades_tab = page.locator("button:has-text('Trades'), [role='tab']:has-text('Trades')").first
            if await trades_tab.is_visible(timeout=2000):
                await trades_tab.click()
                await asyncio.sleep(1)
                scr_path = f"activity_{account_name}_trades.png"
                await page.screenshot(path=scr_path)
                log.info(f"[{account_name}] Screenshot tab Trades tersimpan: {scr_path}")
            
            # Cek tab Activity
            act_tab = page.locator("button:has-text('Activity'), [role='tab']:has-text('Activity')").first
            if await act_tab.is_visible(timeout=2000):
                await act_tab.click()
                await asyncio.sleep(1)
                scr_path = f"activity_{account_name}_activity.png"
                await page.screenshot(path=scr_path)
                log.info(f"[{account_name}] Screenshot tab Activity tersimpan: {scr_path}")
                
            # Cek tab Positions
            pos_tab = page.locator("button:has-text('Positions'), [role='tab']:has-text('Positions')").first
            if await pos_tab.is_visible(timeout=2000):
                await pos_tab.click()
                await asyncio.sleep(1)
                scr_path = f"activity_{account_name}_positions.png"
                await page.screenshot(path=scr_path)
                log.info(f"[{account_name}] Screenshot tab Positions tersimpan: {scr_path}")
        else:
            log.warning(f"[{account_name}] Link profile tidak ditemukan di header/sidebar.")
    except Exception as e:
        log.warning(f"[{account_name}] Gagal cek activity: {e}")


async def place_limit(page, url, name, outcome="up", amount=MIN_TRADE, price=0.50, dry_run=False) -> bool:
    if not await nav_market(page, url, name):
        return False
    try:
        # Switch ke Limit mode
        for sel in ["button.dropdown__trigger", ".otrade-panel__order-type"]:
            try:
                d = page.locator(sel).first
                if await d.is_visible(timeout=2000):
                    await d.click()
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                pass

        for sel in ["li:has-text('Limit')", "button:has-text('Limit')", "[role='menuitem']:has-text('Limit')"]:
            try:
                o = page.locator(sel).first
                if await o.is_visible(timeout=2000):
                    await o.click()
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                pass

        await select_outcome(page, outcome)

        # Set limit price
        for sel in ["input[aria-label='Limit Price']", "input[aria-label*='rice']", ".otrade-panel__limit input"]:
            try:
                li = page.locator(sel).first
                if await li.is_visible(timeout=2000):
                    await li.triple_click()
                    await li.fill(str(price))
                    await asyncio.sleep(0.3)
                    break
            except Exception:
                pass

        await set_amount(page, amount)
        return await click_trade(page, dry_run)
    except Exception as e:
        log.error(f"  Error place limit: {e}")
        return False

async def claim_mainnet_waitlist(page: Page, account_name: str = "main") -> bool:
    """Mengklaim pendaftaran Mainnet Waitlist untuk unlock badge 'First in Line'."""
    try:
        log.info(f"[{account_name}] [QUEST: First in Line] Memeriksa status Mainnet Waitlist...")
        await page.goto(f"{BASE_URL}/mainnet", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2.5)
        
        # Handle welcome modal if present
        for _ in range(4):
            nxt = page.locator("button:has-text('Next')").first
            if await nxt.is_visible(timeout=1500):
                await nxt.click()
                await asyncio.sleep(0.8)
            else:
                break
                
        # Tutup modal close 'x' jika masih ada
        close_btn = page.locator("button[aria-label='Close'], button:has-text('✕')").first
        if await close_btn.is_visible(timeout=1000):
            await close_btn.click()
            await asyncio.sleep(0.5)

        # Klik Join the Waitlist jika tombol terlihat
        wl_btn = page.locator("button:has-text('Join the Waitlist'), button:has-text('Join Waitlist')").first
        if await wl_btn.is_visible(timeout=2500):
            await wl_btn.click()
            await asyncio.sleep(2.0)
            log.info(f"[{account_name}] ✅ [First in Line] Berhasil bergabung ke Mainnet Waitlist!")
            return True
        else:
            log.info(f"[{account_name}] [First in Line] Mainnet Waitlist sudah aktif atau tidak memerlukan input tambahan.")
            return False
    except Exception as e:
        log.warning(f"[{account_name}] Skip claim waitlist: {e}")
        return False

async def place_limit_order(page: Page, outcome: str = "up", price: float = 0.50, shares: int = 100, dry_run: bool = False, account_name: str = "main") -> bool:
    """Pasang limit order secara presisi di trading panel untuk kriteria badge Keeper."""
    try:
        # 1. Pastikan mode Limit aktif di dropdown
        mode_btn = page.locator(".otrade-panel .dropdown__trigger, .otrade-panel button:has-text('Market'), .otrade-panel button:has-text('Limit')").first
        if await mode_btn.is_visible(timeout=3000):
            txt = (await mode_btn.inner_text()).strip()
            if "Limit" not in txt:
                await mode_btn.click(force=True)
                await asyncio.sleep(0.5)
                opt = page.locator("[role='option']:has-text('Limit'), li:has-text('Limit'), button:has-text('Limit'), .dropdown__menu :has-text('Limit')").first
                if await opt.is_visible(timeout=2000):
                    await opt.click(force=True)
                    await asyncio.sleep(0.5)
                    log.info(f"[{account_name}] Mode trading panel diubah ke: Limit")

        # 2. Pastikan Buy tab aktif
        buy_btn = page.locator(".otrade-panel__ptype-btn:has-text('Buy'), button:has-text('Buy')").first
        if await buy_btn.is_visible(timeout=2000):
            cls = await buy_btn.get_attribute("class") or ""
            if "otrade-panel__ptype-btn--on" not in cls and "selected" not in cls:
                await buy_btn.click(force=True)
                await asyncio.sleep(0.5)

        # 3. Pilih Outcome (Up atau Down)
        await select_outcome(page, outcome)
        await asyncio.sleep(0.5)

        # 4. Set Limit Price langsung via DOM & fill (bebas pointer hit-test intercept)
        await page.evaluate('''({ price }) => {
            const inp = document.querySelector("input[aria-label='Limit Price'], input.otrade-panel__limit-input, .otrade-panel input[placeholder*='0.']");
            if (inp) {
                inp.focus();
                inp.value = price;
                inp.dispatchEvent(new Event('input', { bubbles: true }));
                inp.dispatchEvent(new Event('change', { bubbles: true }));
            }
        }''', {"price": f"{price:.2f}"})
        p_inp = page.locator("input[aria-label='Limit Price'], input.otrade-panel__limit-input").first
        if await p_inp.is_visible(timeout=1500):
            try:
                await p_inp.fill(f"{price:.2f}", force=True)
            except Exception:
                pass
        log.info(f"[{account_name}] Limit Price di-set: {price:.2f}")

        # 5. Set Shares (100 shares untuk badge Deep Pockets) via DOM & fill
        await page.evaluate('''({ shares }) => {
            const inp = document.querySelector("input[aria-label='Shares'], input.otrade-panel__amount-input");
            if (inp) {
                inp.focus();
                inp.value = shares;
                inp.dispatchEvent(new Event('input', { bubbles: true }));
                inp.dispatchEvent(new Event('change', { bubbles: true }));
            }
        }''', {"shares": str(int(shares))})
        s_inp = page.locator("input[aria-label='Shares'], input.otrade-panel__amount-input").first
        if await s_inp.is_visible(timeout=1500):
            try:
                await s_inp.fill(str(int(shares)), force=True)
            except Exception:
                pass
        log.info(f"[{account_name}] Limit Shares di-set: {shares}")

        # Juga klik chip +100 jika tersedia
        chip100 = page.locator(".otrade-panel button:has-text('+100'), .otrade-panel button:has-text('100')").first
        if await chip100.is_visible(timeout=1000):
            try:
                await chip100.click(force=True)
                await asyncio.sleep(0.3)
            except Exception:
                pass

        await asyncio.sleep(1.0)
        return await click_trade(page, dry_run=dry_run, account_name=account_name)
    except Exception as e:
        log.warning(f"[{account_name}] Gagal pasang limit order {outcome}: {e}")
        return False

async def run_keeper_two_sided_quote(page: Page, url: str, asset: str, dry_run: bool = False, account_name: str = "main", rest_seconds: int = 40) -> bool:
    """
    Eksekusi Two-Sided Quote (Quote Both Sides):
    Menggunakan fitur resmi '+ Add liquidity' Acepyr untuk membuat quote dua sisi (bid & ask)
    secara otomatis tanpa error Self-Cross!
    - Half-spread 1 tick (spread A0.02 <= 2 sen dari mid -> Razor Thin & Close Quarters)
    - Budget 100+ ACEPYR (Deep Pockets)
    - Resting 40 detik (Clocked In, Hold the Line, Overtime ticks)
    """
    log.info(f"[{account_name}] === KEEPER TWO-SIDED QUOTE ({asset.upper()}) ===")
    if not await nav_market(page, url, asset):
        return False

    try:
        await asyncio.sleep(2)
        
        # 1. Cari tombol + Add liquidity
        liq_btn = page.locator("button:has-text('+ Add liquidity'), button:has-text('Add liquidity'), button:has-text('+ Add Liq')").first
        if await liq_btn.is_visible(timeout=4000):
            log.info(f"[{account_name}] Membuka panel 'Provide Liquidity'...")
            await liq_btn.click(force=True)
            await asyncio.sleep(1.5)
            
            # Cek modal onboarding / Continue jika ada
            cont_btn = page.locator("button:has-text('Continue')").first
            if await cont_btn.is_visible(timeout=1500):
                await cont_btn.click(force=True)
                await asyncio.sleep(1.0)
                
            # Set Budget via chip 25% atau input 100
            chip25 = page.locator("button:has-text('25%')").first
            if await chip25.is_visible(timeout=2000):
                await chip25.click(force=True)
                await asyncio.sleep(0.5)
                log.info(f"[{account_name}] Liquidity Budget di-set via chip: 25%")
            else:
                b_inp = page.locator("input[placeholder*='0'], input[aria-label*='Budget']").first
                if await b_inp.is_visible(timeout=2000):
                    await b_inp.fill("100", force=True)
                    await asyncio.sleep(0.5)
                    log.info(f"[{account_name}] Liquidity Budget di-set via input: 100")

            if dry_run:
                log.info(f"[{account_name}] [DRY RUN] Skip Place quotes.")
                close_btn = page.locator("button[aria-label='Close'], button:has-text('✕')").first
                if await close_btn.is_visible(timeout=1000):
                    await close_btn.click(force=True)
                return True

            # Klik Place quotes
            place_btn = page.locator("button:has-text('Place quotes'), button:has-text('Provide Liquidity')").first
            if await place_btn.is_visible(timeout=3000):
                await place_btn.click(force=True)
                log.info(f"[{account_name}] 🎯 Tombol 'Place quotes' diklik! Menunggu reaksi network...")
                await asyncio.sleep(3)
                
                log.info(f"[{account_name}] 🎯 [Two-Way Street] Quote dua sisi aktif! Menunggu resting ticks ({rest_seconds}s)...")
                await asyncio.sleep(rest_seconds)
                log.info(f"[{account_name}] ✅ Resting selesai. Quote tercatat di order book.")
                return True
            else:
                log.warning(f"[{account_name}] Tombol 'Place quotes' tidak aktif atau tidak ditemukan.")
        else:
            log.warning(f"[{account_name}] Tombol '+ Add liquidity' tidak ditemukan di market {asset}.")
            
        return False
    except Exception as e:
        log.warning(f"[{account_name}] Error keeper quote: {e}")
        return False

async def run_keeper_cycle(page: Page, dry_run: bool = False, account_name: str = "main", tracker: Optional[EligibilityTracker] = None) -> int:
    """
    Menjalankan siklus quest Keeper (Two-Sided Quotes) pada 2 market live crypto berbeda.
    Mentrigger badge:
    - Two-Way Street
    - Close Quarters
    - Razor Thin
    - Deep Pockets
    - Clocked In
    - Hold the Line / Overtime
    - Double Duty (2 markets)
    """
    log.info(f"[{account_name}] === SIKLUS KEEPER QUEST (TWO-SIDED LIMIT QUOTES) ===")
    current_bal = await get_balance(page)
    if not current_bal or current_bal < 100:
        log.info(f"[{account_name}] Balance terlalu rendah untuk quest Keeper ({current_bal}), lewati.")
        return 0

    ts = get_round_timestamps()[1]
    keeper_assets = ["btc", "eth"]
    quotes_success = 0

    for asset in keeper_assets:
        url = build_url("crypto", asset, 60, ts)
        if await run_keeper_two_sided_quote(page, url, asset, dry_run=dry_run, account_name=account_name, rest_seconds=35):
            quotes_success += 1
            if tracker:
                tracker.record_trade(f"keeper_{asset}_{ts}", f"{asset.upper()} TWO-SIDED QUOTE", "keeper", "both", 100.0)
        await asyncio.sleep(3)

    log.info(f"[{account_name}] Siklus Keeper selesai. {quotes_success}/{len(keeper_assets)} market berquote sukses.")
    return quotes_success

# --- Modes ---


async def mode_daily(page: Page, dry_run=False):
    log.info("=" * 55)
    log.info("MODE: DAILY STREAK")
    log.info("=" * 55)
    # Discover active market URLs
    market_urls = await discover_live_markets(page)
    ok = 0
    for url in market_urls:
        outcome = random.choice(["up", "down"])
        log.info(f"[{url.split('/')[-1][:40]}] {outcome.upper()} @ {MIN_TRADE}")
        if await place_order(page, url, "", outcome, MIN_TRADE, dry_run):
            ok += 1
        await asyncio.sleep(WAIT_BETWEEN)
    log.info(f"Daily: {ok}/{len(market_urls)} sukses")

async def mode_multi(page: Page, dry_run=False):
    log.info("=" * 55)
    log.info("MODE: MULTI-MARKET (10 markets)")
    log.info("=" * 55)
    market_urls = await discover_live_markets(page)
    # Ambil 10 market berbeda
    targets = market_urls[:10]
    ok = 0
    for url in targets:
        outcome = random.choice(["up", "down"])
        slug = url.split("/")[-1][:40]
        log.info(f"[{slug}] {outcome.upper()} @ {MIN_TRADE}")
        if await place_order(page, url, slug, outcome, MIN_TRADE, dry_run):
            ok += 1
        await asyncio.sleep(WAIT_BETWEEN)
    log.info(f"Multi-market: {ok}/{len(targets)} sukses")

async def mode_limit(page: Page, dry_run=False):
    log.info("=" * 55)
    log.info("MODE: LIMIT ORDERS (10 markets)")
    log.info("=" * 55)
    market_urls = await discover_live_markets(page)
    targets = market_urls[:10]
    ok = 0
    for url in targets:
        slug = url.split("/")[-1][:40]
        price = round(random.uniform(0.45, 0.55), 2)
        log.info(f"[{slug}] LIMIT UP @ {price} | {MIN_TRADE} ACEPYR")
        if await place_limit(page, url, slug, "up", MIN_TRADE, price, dry_run):
            ok += 1
        await asyncio.sleep(WAIT_BETWEEN)
    log.info(f"Limit orders: {ok}/{len(targets)} sukses")

async def mode_volume(page: Page, dry_run=False):
    log.info("=" * 55)
    log.info("MODE: VOLUME ACCUMULATION (Target: 1000 ACEPYR)")
    log.info("=" * 55)
    market_urls = await discover_live_markets(page)
    rotation = market_urls[:5] if market_urls else []
    total = 0
    target = 1000
    cycle = 0
    while total < target and rotation:
        cycle += 1
        log.info(f"Cycle #{cycle} | Volume: {total}/{target}")
        # Refresh market URLs setiap 5 cycle (bisa berganti round)
        if cycle % 5 == 0:
            rotation = (await discover_live_markets(page))[:5] or rotation
        for url in rotation:
            if total >= target:
                break
            slug = url.split("/")[-1][:40]
            amt = min(10, target - total)
            outcome = random.choice(["up", "down"])
            log.info(f"  [{slug}] {outcome.upper()} @ {amt} (total: {total})")
            if await place_order(page, url, slug, outcome, amt, dry_run):
                total += amt
            await asyncio.sleep(WAIT_BETWEEN)
    log.info(f"Volume: {total} ACEPYR tercapai")

async def mode_profit(page: Page, dry_run=False):
    log.info("=" * 55)
    log.info("MODE: PROFIT POSITIONS")
    log.info("=" * 55)
    market_urls = await discover_live_markets(page)
    targets = market_urls[:3]
    ok = 0
    # Buka UP positions di beberapa market
    for url in targets:
        slug = url.split("/")[-1][:40]
        log.info(f"[{slug}] Buka UP @ {PROFIT_TRADE}")
        if await place_order(page, url, slug, "up", PROFIT_TRADE, dry_run):
            ok += 1
        await asyncio.sleep(WAIT_BETWEEN)
    log.info(f"{ok} posisi dibuka. Market akan auto-settle saat round selesai.")

async def mode_full(page: Page, dry_run=False):
    log.info("=" * 55)
    log.info("MODE: FULL AUTOMATION")
    log.info("=" * 55)
    await mode_multi(page, dry_run)
    await asyncio.sleep(3)
    await mode_limit(page, dry_run)
    await asyncio.sleep(3)
    await mode_profit(page, dry_run)
    await asyncio.sleep(3)
    await mode_volume(page, dry_run)
    log.info("Full automation selesai!")

# --- Meta Markets ---

async def discover_meta_markets(page: Page) -> List[dict]:
    """
    Scrape halaman /meta untuk mendapatkan semua market, konsensus, dan status voting.
    """
    log.info("Mencari market aktif dari /meta...")
    try:
        await page.goto(f"{BASE_URL}/meta", wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector("article.meta-card", timeout=12000)
        except Exception:
            await asyncio.sleep(WAIT_LOAD)
    except Exception as e:
        log.warning(f"Gagal memuat /meta: {e}")
        return []

    # Scroll sedikit untuk trigger lazy load semua cards
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.5)
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)
    except Exception:
        pass

    markets = await page.evaluate('''async () => {
        const cards = [];
        const elements = document.querySelectorAll('article.meta-card, .meta-card, article[class*="meta"], [data-testid*="meta-card"], a[href*="/meta/"]');
        elements.forEach(card => {
            const titleEl = card.querySelector('h1, h2, h3, h4, [class*="title"], [class*="heading"]') || card.querySelector('a') || card;
            const title = titleEl ? titleEl.innerText.trim() : card.innerText.split('\\n')[0].trim();
            if (!title) return;
            
            let slug = '';
            const link = card.tagName === 'A' ? card : card.querySelector('a[href*="/meta/"]');
            if (link && link.href) {
                const parts = link.href.split('/meta/');
                if (parts.length > 1) slug = parts[1].split('?')[0].replace(/\\/+$/, '');
            }
            if (!slug) {
                slug = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
            }
            
            const cardText = card.innerText || '';
            const match = cardText.match(/(\\d{1,3})%\\s*--\\s*Econ/i) || cardText.match(/(\\d{1,3})%/);
            const yesPct = match ? parseInt(match[1]) : 50;
            
            // Cek status vote: jika tombol 'Vote Yes' hilang, ada class --picked, atau tombol disabled
            const hasVoteBtn = !!card.querySelector('button[aria-label="Vote Yes"]');
            const isPicked = !!card.querySelector('.meta-card__btn--picked, .meta-card__btn--vote-cast');
            const isDisabled = !!card.querySelector('.button-3d--disabled, button[disabled]');
            const isVoted = !hasVoteBtn || isPicked || isDisabled;
            
            if (slug && !cards.some(c => c.slug === slug)) {
                cards.push({
                    title: title,
                    slug: slug,
                    url: `https://www.acepyr.com/meta/${slug}`,
                    consensus_yes: yesPct,
                    is_voted: isVoted
                });
            }
        });

        // Fallback fetch jika selector DOM kosong
        if (cards.length === 0) {
            try {
                for (const query of ['type=mirror_scalar&limit=20', 'limit=20']) {
                    const res = await fetch(`/api/markets?${query}`);
                    const json = await res.json();
                    const list = (json && json.data) ? json.data : (Array.isArray(json) ? json : []);
                    list.forEach(item => {
                        const s = item.slug || item.id;
                        if (s && !cards.some(c => c.slug === s)) {
                            cards.push({
                                title: item.title || item.name || s,
                                slug: s,
                                url: `https://www.acepyr.com/meta/${s}`,
                                consensus_yes: Math.round(parseFloat(item.consensusYes || item.yesProbability || 0.5) * 100),
                                is_voted: false
                            });
                        }
                    });
                    if (cards.length > 0) break;
                }
            } catch(e) {}
        }

        return cards;
    }''')

    log.info(f"Ditemukan {len(markets)} Meta Market aktif:")
    for m in markets:
        status_vote = "SUDAH VOTE" if m["is_voted"] else "BELUM VOTE"
        log.info(f"  [{m['slug']}] Yes: {m['consensus_yes']}% | {status_vote} | {m['title'][:35]}")
    return markets

async def select_meta_outcome(page: Page, outcome: str = "yes") -> bool:
    """Pilih Yes atau No pada panel Meta Market."""
    try:
        target_prefix = outcome.strip().lower()
        buttons = await page.locator("aside.otrade__side button.button-3d, .otrade-panel button.button-3d").all()
        for b in buttons:
            txt = (await b.inner_text()).strip().lower()
            if txt.startswith(target_prefix):
                cls = await b.get_attribute("class") or ""
                if "button-3d--unselected" in cls:
                    await b.click()
                    await asyncio.sleep(0.5)
                log.debug(f"  Meta Outcome selected: {outcome.upper()}")
                return True
        log.warning(f"  Tombol meta outcome {outcome} tidak ditemukan")
        return False
    except Exception as e:
        log.warning(f"  Gagal select meta outcome {outcome}: {e}")
        return False

async def mode_meta_vote(page: Page, dry_run: bool = False, account_name: str = "main", tracker: Optional[EligibilityTracker] = None) -> int:
    """
    Opsi 1: Auto-Vote Census di Meta Markets (100% GRATIS).
    Memilih opsi Yes/No sesuai arah konsensus mayoritas.
    """
    if tracker is None:
        tracker = EligibilityTracker(account_name)

    log.info(f"[{account_name}] === SIKLUS META CENSUS VOTE (GRATIS) ===")
    markets = await discover_meta_markets(page)
    if not markets:
        log.warning(f"[{account_name}] Tidak ada market meta ditemukan.")
        return 0

    unvoted = [m for m in markets if not m["is_voted"]]
    if not unvoted:
        log.info(f"[{account_name}] [META CENSUS] Semua market sudah di-vote ({len(markets)}/{len(markets)}).")
        return 0

    log.info(f"[{account_name}] [META CENSUS] Ditemukan {len(unvoted)} market yang belum di-vote. Mulai voting...")
    
    votes_queued = 0
    queued_details = []
    for m in unvoted:
        # Arah vote: ikuti konsensus mayoritas
        if m["consensus_yes"] > 50:
            target_side = "Yes"
        elif m["consensus_yes"] < 50:
            target_side = "No"
        else:
            target_side = random.choice(["Yes", "No"])
            
        try:
            card_locator = page.locator("article.meta-card").filter(has_text=m["title"]).first
            btn = card_locator.locator(f"button[aria-label='Vote {target_side}']").first
            if await btn.is_visible(timeout=1000):
                if await btn.is_disabled():
                    log.info(f"[{account_name}]   [Skip] Tombol vote {m['slug']} disabled (sudah ditutup/dicast).")
                    continue
                cls = await btn.get_attribute("class") or ""
                if "disabled" in cls or "picked" in cls or "vote-cast" in cls:
                    log.info(f"[{account_name}]   [Skip] Vote {m['slug']} sudah terpasang.")
                    continue
                await btn.click(timeout=1500)
                votes_queued += 1
                queued_details.append((m["slug"], m["title"], target_side))
                log.info(f"[{account_name}]   + Vote {target_side.upper()} di '{m['title'][:30]}' (Konsensus: {m['consensus_yes']}%)")
                await asyncio.sleep(0.3)
        except Exception as e:
            log.warning(f"[{account_name}]   Gagal klik vote {m['slug']}: {e}")

    if votes_queued == 0:
        log.warning(f"[{account_name}] Tidak ada vote yang masuk ke antrean.")
        return 0

    try:
        cast_btn = page.locator("button:has-text('Cast Vote'), button:has-text('Cast ')").first
        if await cast_btn.is_visible(timeout=3000):
            if dry_run:
                log.info(f"[{account_name}] [DRY RUN] Skip Cast Vote ({votes_queued} votes). Reset sidebar...")
                clear_btn = page.locator("button:has-text('Clear All')").first
                if await clear_btn.is_visible(timeout=1000):
                    await clear_btn.click()
                return votes_queued
            else:
                try:
                    await cast_btn.click(force=True, timeout=3000)
                except Exception:
                    await page.evaluate("el => el.click()", cast_btn)
                await asyncio.sleep(2)
                log.info(f"[{account_name}] [META CENSUS] Sukses Cast {votes_queued} Votes!")
                for slug, title, side in queued_details:
                    tracker.record_vote(slug, title, side)
                return votes_queued
        else:
            log.warning(f"[{account_name}] Tombol Cast Vote tidak ditemukan di sidebar.")
    except Exception as e:
        log.error(f"[{account_name}] Error saat Cast Vote: {e}")
    return 0

async def mode_meta_trade(page: Page, dry_run: bool = False, bet_history: set = set(), account_name: str = "main", tracker: Optional[EligibilityTracker] = None) -> int:
    """
    Opsi 2: Money Line Trading di Meta Markets.
    Menembak hasil dengan probabilitas / konsensus tinggi (>= 68%) dengan sizing dinamis & aman.
    Setiap trade baru dicatat ke EligibilityTracker untuk memaksimalkan distinct markets.
    """
    if tracker is None:
        tracker = EligibilityTracker(account_name)

    log.info(f"[{account_name}] === SIKLUS META MONEY LINE TRADE ===")
    markets = await discover_meta_markets(page)
    if not markets:
        return 0

    candidates = []
    for m in markets:
        market_id = f"meta_{m['slug']}"
        if tracker.has_traded_market(market_id) or market_id in bet_history:
            continue

        yes_pct = m["consensus_yes"]
        no_pct = 100 - yes_pct

        if yes_pct >= no_pct:
            fav_outcome = "yes"
            confidence = yes_pct
        else:
            fav_outcome = "no"
            confidence = no_pct

        # Filter: Market dengan confidence >= 68%
        if confidence >= 68:
            candidates.append((m, fav_outcome, confidence, market_id))

    candidates.sort(key=lambda x: x[2], reverse=True)

    if not candidates:
        log.info(f"[{account_name}] [META TRADE] Tidak ada market baru dengan confidence >= 68%. Lewati.")
        return 0

    current_bal = await get_balance(page)
    if not current_bal or current_bal < 10:
        log.warning(f"[{account_name}] [META TRADE] Saldo minim ({current_bal}), lewati meta trade.")
        return 0

    log.info(f"[{account_name}] [META TRADE] Ditemukan {len(candidates)} market kandidat baru:")
    for m, outcome, conf, mid in candidates:
        log.info(f"   -> [{m['slug']}] {outcome.upper()} ({conf}%)")

    # Anti-sybil: acak urutan top candidate agar antar akun tidak copy-trade identik
    chosen_candidates = select_natural_candidates(candidates, max_picks=2)

    trades_executed = 0
    for m, fav_outcome, confidence, market_id in chosen_candidates:
        cur_bal = await get_balance(page)
        if not cur_bal or cur_bal < 10:
            break

        # Sizing dinamis & aman (menjaga bankroll dan menghindari pola kaku)
        trade_amt = calculate_safe_bet_amount(cur_bal, confidence)

        log.info(f"[{account_name}] [META TRADE] Buka: {m['title']} | Bet {fav_outcome.upper()} ({confidence}%) @ {trade_amt} ACEPYR")

        try:
            await page.goto(m["url"], wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(WAIT_LOAD)

            # Cek jika form trade belum terbuka (masih berupa vote di sidebar)
            trade_submit = page.locator("aside.otrade__side button.otrade-panel__submit").first
            if not await trade_submit.is_visible(timeout=2000):
                log.info(f"[{account_name}] Form trade belum terbuka, cast vote census {fav_outcome.upper()} di detail...")
                census_btn = page.locator(f"button.ometa-dv__btn[aria-label='Vote {fav_outcome.capitalize()}'], button[aria-label='Vote {fav_outcome.capitalize()}']").first
                if await census_btn.is_visible(timeout=3000):
                    await census_btn.click()
                    await asyncio.sleep(0.8)
                    cast_b = page.locator("aside.otrade__side button:has-text('Cast Vote'), button:has-text('Cast ')").first
                    if await cast_b.is_visible(timeout=3000):
                        await cast_b.click()
                        await asyncio.sleep(3)

            # Pastikan Buy mode aktif
            buy_btn = page.locator(".otrade-panel__ptype-btn:has-text('Buy')").first
            if await buy_btn.is_visible(timeout=2000):
                cls = await buy_btn.get_attribute("class") or ""
                if "otrade-panel__ptype-btn--on" not in cls:
                    await buy_btn.click()
                    await asyncio.sleep(0.5)

            # Pilih Yes atau No di Money Line
            selected = await select_meta_outcome(page, fav_outcome)
            if not selected:
                log.warning(f"[{account_name}] Gagal memilih outcome {fav_outcome}")
                continue

            # Set amount
            await set_amount(page, trade_amt)

            # Klik Trade
            if await click_trade(page, dry_run, account_name=account_name):
                trades_executed += 1
                bet_history.add(market_id)
                tracker.record_trade(market_id, m["title"], "meta", fav_outcome, trade_amt)
                log.info(f"[{account_name}] [META TRADE] Sukses bet {trade_amt} ACEPYR di {m['title']}!")

            # Random jitter jeda
            jitter = random.uniform(2.0, 5.0)
            await asyncio.sleep(WAIT_BETWEEN + jitter)
        except Exception as e:
            log.error(f"[{account_name}] Error trade meta {m['slug']}: {e}")

    log.info(f"[{account_name}] [META TRADE] Selesai: {trades_executed} trade dieksekusi.")
    return trades_executed


async def mode_meta(page: Page, dry_run: bool = False):
    """Jalankan siklus lengkap Meta Markets (Census Vote + Money Line Trade)."""
    log.info("=" * 55)
    log.info("MODE: META MARKETS (CENSUS VOTE + MONEY LINE)")
    log.info("=" * 55)
    await mode_meta_vote(page, dry_run, account_name="main")
    await asyncio.sleep(2)
    history = set()
    try:
        import os, json
        if os.path.exists("bet_history_main.json"):
            with open("bet_history_main.json", "r") as f:
                history = set(json.load(f))
    except Exception:
        pass
    await mode_meta_trade(page, dry_run, history, account_name="main")

async def mode_meta_vote_standalone(page: Page, dry_run: bool = False):
    await mode_meta_vote(page, dry_run, account_name="main")

async def mode_meta_trade_standalone(page: Page, dry_run: bool = False):
    history = set()
    try:
        import os, json
        if os.path.exists("bet_history_main.json"):
            with open("bet_history_main.json", "r") as f:
                history = set(json.load(f))
    except Exception:
        pass
    await mode_meta_trade(page, dry_run, history, account_name="main")

async def run_growth_cycle(page, dry_run, bet_history=None, account_name="main", tracker: Optional[EligibilityTracker] = None):
    if tracker is None:
        tracker = EligibilityTracker(account_name)
    if bet_history is None:
        bet_history = tracker.get_distinct_market_ids()

    # Inisialisasi ok counter di awal agar tidak UnboundLocalError
    ok = 0
    ALLOWED_REAL_CRYPTO = {"btc", "eth", "sol", "xrp", "bnb", "hype"}

    # 0. Tampilkan Status Eligibility Airdrop
    tracker.print_dashboard()

    # 1. Jalankan Siklus /live terlebih dahulu (Prioritas Utama Testnet: Crypto 60m)
    ts = get_round_timestamps()[1]
    log.info(f"[{account_name}] Scraping live crypto markets dari API (Jam: {ts})...")
    try:
        await page.goto(f"{BASE_URL}/live", wait_until="domcontentloaded")
        await asyncio.sleep(WAIT_LOAD)
    except Exception as e:
        log.warning(f"Gagal memuat halaman live: {e}")
        return
        
    # Ambil data market langsung dari internal API Acepyr (KHUSUS Crypto 60m)
    # Catatan: Stock/Commodity (NVDA, SPCX, TSLA, SPY, GOLD, dll) di Acepyr testnet adalah 'Preview-only / No live market wired'
    api_markets = []
    try:
        for family in ["crypto"]:
            for window in [60]:
                api_res = await page.evaluate(f'''async () => {{
                    try {{
                        const r = await fetch('/api/markets/live?family={family}&window={window}&windows=12');
                        return await r.json();
                    }} catch(e) {{ return null; }}
                }}''')
                if api_res and api_res.get('success'):
                    for item in api_res.get('data', []):
                        symbol = (item.get('symbol') or '').lower()
                        if symbol not in ALLOWED_REAL_CRYPTO:
                            continue
                        active = item.get('active')
                        if active and active.get('slug'):
                            up_prob = float(active.get('upProbability', 0.5))
                            slug = active.get('slug')
                            if up_prob >= 0.5:
                                direction = "up"
                                conf = int(round(up_prob * 100))
                            else:
                                direction = "down"
                                conf = int(round((1.0 - up_prob) * 100))
                            
                            api_markets.append({
                                "asset": symbol,
                                "direction": direction,
                                "confidence": conf,
                                "market_id": slug,
                                "duration": window,
                                "exact_url": f"{BASE_URL}/live/{slug}"
                            })
    except Exception as e:
        log.warning(f"Gagal fetch live markets dari API: {e}")

    # Fallback ke regex jika API kosong (Hanya untuk ALLOWED_REAL_CRYPTO)
    if not api_markets:
        try:
            html = await page.evaluate("() => document.body.innerHTML")
            cards = re.findall(r'([A-Z]+(?:\s+PRICE)?\s+UP\s*(?:\/|OR)\s*DOWN\s*(?:NEXT\s*)?\d*\s*(?:HOUR|HR)?).*?(\d{1,3})%.*?(Up|Down)', html, re.IGNORECASE | re.DOTALL)
            for title, pct_str, direction in cards:
                pct = int(pct_str)
                if pct == 0:
                    continue
                asset = title.split(' ')[0].lower()
                if asset not in ALLOWED_REAL_CRYPTO:
                    continue
                dur_match = re.search(r'(\d+)\s*(?:HR|HOUR)', title, re.IGNORECASE)
                duration = int(dur_match.group(1)) * 60 if dur_match else 60
                api_markets.append({
                    "asset": asset,
                    "direction": direction.lower(),
                    "confidence": pct,
                    "market_id": f"{asset}_{ts}",
                    "duration": duration,
                    "exact_url": f"{BASE_URL}/live/crypto-{asset}-up-or-down-{duration}min-{ts}"
                })
        except Exception:
            pass

    # Filter distinct crypto market yang belum pernah ditradingkan akun ini
    seen_assets = set()
    high_prob_markets = []
    
    for m in api_markets:
        asset = m["asset"]
        if asset not in ALLOWED_REAL_CRYPTO:
            continue
        if asset in seen_assets:
            continue
        seen_assets.add(asset)
        
        market_id = m["market_id"]
        # Prioritas: hanya yang belum pernah dipasang di ronde ini (mengejar distinct markets)
        if tracker.has_traded_market(market_id) or market_id in bet_history:
            continue
            
        conf = m["confidence"]
        direction = m["direction"]
        duration = m["duration"]
        exact_url = m["exact_url"]
        
        log.info(f"[{account_name}] Cek Token: {asset.upper()} -> {conf}% {direction.upper()} (dur={duration}min)")
        high_prob_markets.append((asset, direction, conf, market_id, duration, exact_url))
            
    # Urutkan dari probabilitas tertinggi
    high_prob_markets.sort(key=lambda x: x[2], reverse=True)
    
    if not high_prob_markets:
        log.info(f"[{account_name}] Semua live crypto market jam ini ({ts}) sudah ditradingkan oleh akun ini.")
    else:
        # Anti-sybil: pilih dari seluruh kandidat, target 2 trade sukses
        # Urutkan dari probabilitas tertinggi, iterasi semua hingga 2 sukses (fallback ke market berikutnya jika panel closing)
        target_trades = 2
        attempted = set()
        log.info(f"[{account_name}] Ditemukan {len(high_prob_markets)} kandidat market live crypto. Target {target_trades} trade:")
        
        for asset, outcome, pct, market_id, duration, exact_url in high_prob_markets:
            if ok >= target_trades:
                break
            if market_id in attempted:
                continue
            attempted.add(market_id)
            
            current_bal = await get_balance(page)
            if not current_bal or current_bal <= 0:
                log.warning(f"[{account_name}] Balance 0 atau tidak terbaca, skip live trades.")
                break
                
            # Sizing aman & dinamis (menjaga bankroll, bukan flat multiplier kaku)
            trade_amt = calculate_safe_bet_amount(current_bal, pct)
            url = exact_url or build_url("crypto", asset, duration, ts)
                
            log.info(f"[{account_name}] [{asset.upper()}] Probabilitas {outcome.upper()} = {pct}% | Balance: {current_bal:,.2f} -> Bet {trade_amt} ACEPYR")
            
            if await place_order(page, url, asset, outcome, trade_amt, dry_run, account_name=account_name):
                ok += 1
                bet_history.add(market_id)
                tracker.record_trade(market_id, f"{asset.upper()} {outcome.upper()}", "live", outcome, trade_amt)
            else:
                log.warning(f"[{account_name}] [{asset.upper()}] Gagal place order, mencoba kandidat berikutnya...")
                
            # Random delay untuk humanizer
            jitter = random.uniform(2.5, 6.0)
            await asyncio.sleep(WAIT_BETWEEN + jitter)
            
        log.info(f"[{account_name}] Siklus live selesai. {ok}/{min(target_trades, len(high_prob_markets))} trade sukses.")

    # 2. Cek Meta Markets secara cepat (hanya jika ada yang belum di-vote)
    try:
        await mode_meta_vote(page, dry_run, account_name=account_name, tracker=tracker)
    except Exception as e:
        log.warning(f"[{account_name}] Skip Meta Markets: {e}")

    # 2.5 Cek & Jalankan Mainnet Waitlist (Badge: First in Line)
    try:
        await claim_mainnet_waitlist(page, account_name=account_name)
    except Exception as e:
        log.warning(f"[{account_name}] Skip Mainnet Waitlist: {e}")

    # 2.6 Cek & Jalankan Keeper Quota (Two-Sided Limit Quotes untuk 9 Badge Keeper)
    try:
        await run_keeper_cycle(page, dry_run=dry_run, account_name=account_name, tracker=tracker)
    except Exception as e:
        log.warning(f"[{account_name}] Skip Keeper Cycle: {e}")

    # 3. Periksa Menu Activity di Profile jika ada trade yang berhasil dipasang
    if ok > 0:
        try:
            await check_user_activity(page, account_name=account_name)
        except Exception as e:
            log.warning(f"[{account_name}] Error saat cek activity: {e}")

    # 4. Tampilkan Status Eligibility Terkini Setelah Siklus
    tracker.print_dashboard()

async def mode_growth(page: Page, dry_run: bool = False):
    """
    GROWTH & ELIGIBILITY MODE
    Mengejar target airdrop testnet: Active Days, Distinct Markets, dan volume terukur.
    """
    tracker = EligibilityTracker("main")
    bet_history = tracker.get_distinct_market_ids()
        
    while True:
        await run_growth_cycle(page, dry_run, bet_history, account_name="main", tracker=tracker)
        log.info("Menunggu 2 menit sebelum scan ulang...")
        await asyncio.sleep(120)

# --- Browser Setup ---

async def setup(playwright: Playwright):
    """Hubungkan ke Chrome yang sudah berjalan, atau launch baru."""
    # Opsi 1: Connect ke Chrome via CDP (Chrome harus dijalankan dengan --remote-debugging-port=9222)
    try:
        log.info("Mencoba connect ke Chrome (port 9222)...")
        browser = await playwright.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        pages = ctx.pages
        page = pages[0] if pages else await ctx.new_page()
        log.info(f"Connected! Tab: {await page.title()}")
        return browser, page
    except Exception as e:
        log.warning(f"CDP gagal: {e}")

    # Opsi 2: Launch browser baru dengan profil persistent
    log.info("Launch browser baru (profil persistent)...")
    ctx = await playwright.chromium.launch_persistent_context(
        user_data_dir="./browser_profile",
        headless=False,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        viewport={"width": 1280, "height": 800},
    )
    page = await ctx.new_page()
    await page.goto(f"{BASE_URL}/faucet", wait_until="domcontentloaded")
    await asyncio.sleep(3)

    bal = await get_balance(page)
    if bal and bal > 0:
        log.info(f"Sudah login! Balance: {bal}")
    else:
        log.warning("Belum login! Silakan login di browser yang terbuka...")
        log.warning("Tunggu 60 detik...")
        await asyncio.sleep(60)

    return ctx, page

# --- Main ---

async def main():
    parser = argparse.ArgumentParser(description="Acepyr Faucet Bot")
    parser.add_argument("--mode", choices=["daily","multi","limit","volume","profit","growth","meta_vote","meta_trade","meta","full"], default="full")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log.info("=== ACEPYR FAUCET BOT ===")
    log.info(f"Mode: {args.mode} | Dry-run: {args.dry_run}")

    async with async_playwright() as pw:
        browser, page = await setup(pw)
        try:
            await page.goto(f"{BASE_URL}/faucet", wait_until="domcontentloaded")
            await asyncio.sleep(3)
            bal = await get_balance(page)
            log.info(f"Balance awal: {bal} ACEPYR")

            modes = {
                "daily":      mode_daily,
                "multi":      mode_multi,
                "limit":      mode_limit,
                "volume":     mode_volume,
                "profit":     mode_profit,
                "growth":     mode_growth,
                "meta_vote":  mode_meta_vote_standalone,
                "meta_trade": mode_meta_trade_standalone,
                "meta":       mode_meta,
                "full":       mode_full,
            }
            await modes[args.mode](page, args.dry_run)

            bal2 = await get_balance(page)
            log.info(f"Balance akhir: {bal2} ACEPYR")
            log.info("Bot selesai! Cek /faucet untuk progress.")
        except KeyboardInterrupt:
            log.info("Bot dihentikan.")
        except Exception as e:
            log.error(f"Error: {e}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(main())
