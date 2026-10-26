import argparse
import asyncio
import os
import json
import logging
import random
from playwright.async_api import async_playwright
from bot import run_growth_cycle, get_balance, BASE_URL
from eligibility import EligibilityTracker

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger(__name__)

async def main():
    parser = argparse.ArgumentParser(description="Multi-account automated prediction trading engine.")
    parser.add_argument("--accounts", type=str, default="acc1,acc2,acc3,acc4,acc5,acc6,acc7,acc8,acc9", help="List akun dipisah koma (default: acc1 s/d acc9)")
    parser.add_argument("--dry-run", action="store_true", help="Jalankan tanpa benar-benar klik Trade")
    args = parser.parse_args()
    
    account_names = [a.strip() for a in args.accounts.split(",") if a.strip()]
    if not account_names:
        log.error("Tidak ada akun yang ditentukan!")
        return
        
    log.info(f"=== CEPIRIT MULTI-ACCOUNT TRADING ENGINE ===")
    log.info(f"Total akun aktif ({len(account_names)} akun): {', '.join(account_names)}")
    log.info(f"Dry-run: {args.dry_run}")
    
    profiles_dir = os.path.join(os.getcwd(), "profiles")
    os.makedirs(profiles_dir, exist_ok=True)
    
    # Inisialisasi Eligibility Tracker untuk setiap akun
    trackers = {acc: EligibilityTracker(acc) for acc in account_names}
    
    log.info("\n>>> STATUS ELIGIBILITY AIRDROP AWAL:")
    for acc in account_names:
        trackers[acc].print_dashboard()
    
    async with async_playwright() as pw:
        contexts = {}
        pages = {}
        
        # 1. Launch semua browser profile
        for account in account_names:
            profile_path = os.path.join(profiles_dir, account)
            log.info(f"[{account}] Membuka profile browser...")
            
            # Launch persistent context
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=profile_path,
                headless=False, # Supaya user bisa lihat/login jika diperlukan
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--window-size=1000,800'
                ]
            )
            contexts[account] = context
            page = context.pages[0] if context.pages else await context.new_page()
            pages[account] = page
            
            # Navigate ke live page untuk trigger login check
            await page.goto(f"{BASE_URL}/live", wait_until="domcontentloaded")
            await asyncio.sleep(1)
            
        # Cek status login setiap akun secara otomatis
        log.info("=========================================")
        log.info("MEMERIKSA STATUS LOGIN SEMUA AKUN...")
        unlogged = []
        for account in account_names:
            bal = await get_balance(pages[account])
            if bal is not None and bal > 0:
                log.info(f"[{account}] ✅ Login terdeteksi! Balance: {bal:,.2f} CPRT")
            else:
                unlogged.append(account)
                log.warning(f"[{account}] ⚠️ Belum login! Silakan login di jendela browser [{account}].")

        if unlogged:
            log.info("=========================================")
            log.info(f"Akun yang belum ter-login: {', '.join(unlogged)}")
            log.info("Bot menunggu hingga Anda login (atau hingga 60 detik)...")
            log.info("=========================================")
            for _ in range(30):
                still_unlogged = []
                for acc in unlogged:
                    bal = await get_balance(pages[acc])
                    if bal is None or bal <= 0:
                        still_unlogged.append(acc)
                    else:
                        log.info(f"[{acc}] ✅ Berhasil login! Balance: {bal:,.2f} CPRT")
                unlogged = still_unlogged
                if not unlogged:
                    log.info("Semua akun berhasil login!")
                    break
                await asyncio.sleep(2)
                
            # Jika setelah ditunggu masih ada yang belum login, tutup dan lewati akun tersebut
            if unlogged:
                log.warning(f"Akun berikut belum ter-login dan DITUTUP/DILEWATI otomatis: {', '.join(unlogged)}")
                for acc in unlogged:
                    try:
                        await contexts[acc].close()
                    except Exception:
                        pass
                    contexts.pop(acc, None)
                    pages.pop(acc, None)
                    trackers.pop(acc, None)
                account_names = [a for a in account_names if a not in unlogged]
                log.info(f"Melanjutkan siklus hanya dengan akun yang aktif ({len(account_names)} akun): {', '.join(account_names)}")
        else:
            log.info("Semua akun sudah terdeteksi login. Mulai siklus...")
            await asyncio.sleep(3)
        log.info("=========================================")
        
        # 2. Infinite Loop: Putar bergantian setiap akun
        while True:
            for account in account_names:
                page = pages[account]
                tracker = trackers[account]
                history = tracker.get_distinct_market_ids()
                
                log.info(f"\n---> MENJALANKAN SIKLUS UNTUK AKUN: [{account.upper()}] <---")
                try:
                    await run_growth_cycle(page, args.dry_run, history, account_name=account, tracker=tracker)
                except Exception as e:
                    log.error(f"[{account}] Error saat run cycle: {e}")
                    
                # Anti-sybil: jeda acak antar akun (6 - 15 detik) agar aksi tidak identik serentak
                stagger_delay = random.uniform(6.0, 15.0)
                await asyncio.sleep(stagger_delay)
                    
            sleep_time = random.randint(110, 150)
            log.info(f"\nSemua akun selesai satu putaran. Menunggu {sleep_time} detik sebelum scan ulang...")
            await asyncio.sleep(sleep_time)

if __name__ == "__main__":
    asyncio.run(main())
