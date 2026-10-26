"""
Cepirit Airdrop Eligibility Tracker & Anti-Sybil Optimizer
=========================================================
Kriteria penilaian Testnet & Genesis:
1. Days on which you traded (Active Days) - Bobot 50%
2. Distinct markets traded (Variasi Market) - Bobot 50%
3. Anti-Sybil & Anti-Bot Protection (Mencegah diskualifikasi akun clone/bot)
"""

import os
import json
import random
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

log = logging.getLogger(__name__)

class EligibilityTracker:
    def __init__(self, account_name: str = "main"):
        self.account_name = account_name
        self.file_path = f"eligibility_{account_name}.json"
        self.legacy_history_path = f"bet_history_{account_name}.json"
        self.data: Dict[str, Any] = self._load()

    def _get_utc_today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> Dict[str, Any]:
        default_data = {
            "account": self.account_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "active_days": [],             # List tanggal unik YYYY-MM-DD
            "distinct_markets": {},        # market_id -> metadata
            "daily_trades": {},            # YYYY-MM-DD -> list of trades
            "total_volume_acepyr": 0.0,
            "total_trades_count": 0,
            "census_votes_count": 0
        }

        # Coba load file eligibility yang sudah ada
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data
            except Exception as e:
                log.warning(f"[{self.account_name}] Gagal membaca {self.file_path}: {e}")

        # Jika belum ada file eligibility, migrasikan data dari bet_history_{account}.json
        if os.path.exists(self.legacy_history_path):
            try:
                import re
                with open(self.legacy_history_path, "r", encoding="utf-8") as f:
                    legacy_list = json.load(f)
                    migrated_days = set()
                    for mid in legacy_list:
                        m_str = str(mid)
                        m_type = "meta" if m_str.startswith("meta_") else "live"
                        default_data["distinct_markets"][m_str] = {
                            "type": m_type,
                            "migrated": True
                        }
                        # Ekstrak tanggal jika ada format YYYYMMDD di slug (misal 20261006...)
                        date_match = re.search(r'(202\d{5})', m_str)
                        if date_match:
                            d_raw = date_match.group(1) # YYYYMMDD
                            formatted_date = f"{d_raw[:4]}-{d_raw[4:6]}-{d_raw[6:8]}"
                            migrated_days.add(formatted_date)

                    default_data["active_days"] = sorted(list(migrated_days))
                    default_data["total_trades_count"] = len(legacy_list)
                    # Estimasi rata-rata volume historis jika belum ada log amount
                    default_data["total_volume_acepyr"] = float(len(legacy_list) * 15.0)
                    log.info(f"[{self.account_name}] Sukses migrasi {len(legacy_list)} market & {len(migrated_days)} hari aktif dari legacy.")
            except Exception as e:
                log.warning(f"[{self.account_name}] Gagal migrasi legacy history: {e}")

        return default_data

    def save(self):
        self.data["last_updated"] = datetime.now(timezone.utc).isoformat()
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
                
            # Sinkronkan juga ke legacy bet_history_{account}.json untuk kompatibilitas
            legacy_keys = list(self.data["distinct_markets"].keys())
            with open(self.legacy_history_path, "w", encoding="utf-8") as f:
                json.dump(legacy_keys, f)
        except Exception as e:
            log.warning(f"[{self.account_name}] Gagal menyimpan eligibility data: {e}")

    def record_trade(self, market_id: str, title: str, market_type: str, outcome: str, amount: float):
        """Catat transaksi trade sukses."""
        today = self._get_utc_today()
        now_iso = datetime.now(timezone.utc).isoformat()

        # 1. Update Active Days
        if today not in self.data["active_days"]:
            self.data["active_days"].append(today)
            self.data["active_days"].sort()

        # 2. Update Distinct Markets
        is_new_market = market_id not in self.data["distinct_markets"]
        self.data["distinct_markets"][market_id] = {
            "title": title,
            "type": market_type,
            "outcome": outcome,
            "last_amount": amount,
            "last_traded_at": now_iso
        }

        # 3. Update Daily Trades
        if today not in self.data["daily_trades"]:
            self.data["daily_trades"][today] = []
        self.data["daily_trades"][today].append({
            "market_id": market_id,
            "title": title,
            "type": market_type,
            "outcome": outcome,
            "amount": amount,
            "time": now_iso
        })

        # 4. Update Volume & Total Count
        self.data["total_volume_acepyr"] += float(amount)
        self.data["total_trades_count"] += 1

        self.save()

        if is_new_market:
            log.info(f"[{self.account_name}] 🎯 NEW DISTINCT MARKET #{len(self.data['distinct_markets'])}: [{market_type.upper()}] {title}")
        log.info(f"[{self.account_name}] 🗓 ACTIVE DAY STATUS: {len(self.data['active_days'])} hari aktif tercatat.")

    def record_vote(self, market_slug: str, title: str, outcome: str):
        """Catat vote Census Meta Market (100% free)."""
        self.data["census_votes_count"] = self.data.get("census_votes_count", 0) + 1
        self.save()

    def has_traded_market(self, market_id: str) -> bool:
        return market_id in self.data["distinct_markets"]

    def get_distinct_market_ids(self) -> set:
        return set(self.data["distinct_markets"].keys())

    def get_today_trades_count(self) -> int:
        today = self._get_utc_today()
        return len(self.data.get("daily_trades", {}).get(today, []))

    def is_today_active(self) -> bool:
        return self._get_utc_today() in self.data["active_days"]

    def get_stats(self) -> Dict[str, Any]:
        distinct_count = len(self.data["distinct_markets"])
        active_days_count = len(self.data["active_days"])
        trades_today = self.get_today_trades_count()
        vol = self.data["total_volume_acepyr"]
        
        # Hitung live vs meta
        live_count = sum(1 for m in self.data["distinct_markets"].values() if m.get("type") == "live")
        meta_count = sum(1 for m in self.data["distinct_markets"].values() if m.get("type") == "meta")

        # Estimasi tier berdasarkan perbandingan median (docs Genesis)
        if active_days_count >= 14 and distinct_count >= 100:
            tier = "Tier 1 (Diamond Contributor - Top 5%)"
        elif active_days_count >= 7 and distinct_count >= 50:
            tier = "Tier 2 (Gold Contributor - Top 15%)"
        elif active_days_count >= 3 and distinct_count >= 20:
            tier = "Tier 3 (Silver Contributor - Top 30%)"
        else:
            tier = "Tier 4 (Early Active)"

        return {
            "account": self.account_name,
            "active_days": active_days_count,
            "distinct_markets": distinct_count,
            "live_markets": live_count,
            "meta_markets": meta_count,
            "trades_today": trades_today,
            "is_today_active": self.is_today_active(),
            "total_volume": vol,
            "total_trades": self.data["total_trades_count"],
            "census_votes": self.data.get("census_votes_count", 0),
            "tier_estimate": tier
        }

    def print_dashboard(self):
        s = self.get_stats()
        today_mark = "[V] SUDAH AKTIF" if s["is_today_active"] else "[!] BELUM TRADE HARI INI"
        
        lines = [
            "\n" + "=" * 65,
            f" [*] AIRDROP ELIGIBILITY MONITOR -- [{s['account'].upper()}]",
            "=" * 65,
            f" - Hari Aktif (Active Days)     : {s['active_days']} hari  {today_mark}",
            f" - Variasi Market (Distinct)    : {s['distinct_markets']} total ({s['live_markets']} Live, {s['meta_markets']} Meta)",
            f" - Trade Hari Ini (Today)       : {s['trades_today']} trade dieksekusi",
            f" - Census Oracle Votes          : {s['census_votes']} votes",
            f" - Akumulasi Volume             : {s['total_volume']:,.1f} $CPRT",
            f" - Status Estimasi Tier         : {s['tier_estimate']}",
            "=" * 65 + "\n"
        ]
        text = "\n".join(lines)
        try:
            print(text)
        except Exception:
            # Fallback for strict encodings
            print(text.encode('ascii', errors='replace').decode('ascii'))


# --- Anti-Sybil & Human-Like Helpers ---

def calculate_safe_bet_amount(current_balance: float, confidence: int = 70) -> int:
    """
    Kalkulasi ukuran taruhan dinamis & natural:
    - Tidak kaku di satu angka (menghindari pola bot seragam).
    - Menjaga bankroll akun agar saldo tidak habis.
    - Tetap memenuhi kriteria volume.
    """
    if current_balance <= 0:
        return 1

    # Jika saldo menipis (< 30), gunakan ukuran minimal 1-2 ACEPYR untuk jaga streak
    if current_balance < 30:
        return random.choice([1, 2])

    # Base risk: 2.5% s/d 4.0% balance
    pct = random.uniform(0.025, 0.040)
    base_amt = current_balance * pct

    # Sesuaikan dengan confidence
    if confidence >= 85:
        base_amt *= 1.2
    elif confidence <= 68:
        base_amt *= 0.85

    # Random jitter integer antara 8 s/d 35 ACEPYR
    raw_amt = int(round(base_amt))
    clamped_amt = max(5, min(raw_amt, 35))
    
    # Tambahkan jitter acak +- 1 sampai 3 ACEPYR agar tiap akun punya nilai berbeda
    jitter = random.choice([-2, -1, 0, 1, 2, 3])
    final_amt = max(3, clamped_amt + jitter)
    return final_amt

def select_natural_candidates(candidates: List[Any], max_picks: int = 2) -> List[Any]:
    """
    Mengacak sedikit urutan pemilihan kandidat market agar antar akun
    tidak menembak market yang sama persis secara serentak (anti-copy trading).
    """
    if not candidates:
        return []
    if len(candidates) <= max_picks:
        return candidates

    # Ambil pool kandidat terbaik (top 4-5) lalu acak urutannya
    top_pool = candidates[:min(len(candidates), 5)]
    random.shuffle(top_pool)
    return top_pool[:max_picks]
