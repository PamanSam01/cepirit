# Cepirit - Prediction Market & Testnet Engine
Python automation engine for multi-account trading, streak preservation, and testnet reward optimization on decentralized prediction markets.

## Testnet Criteria Optimization
1. **Active Days (50%)**: Maximizes active calendar days traded across all accounts.
2. **Distinct Markets (50%)**: Maximizes unique prediction markets (Crypto & Meta Census).
3. **Anti-Sybil Architecture**: Natural jitter intervals, dynamic bankroll sizing, and candidate randomization to prevent copy-trading detection.

## Installation
```bash
pip install -r requirements.txt
playwright install chromium
```

## Usage

### 1. Multi-Account Execution (Default):
```bash
# Runs 9 accounts (acc1 to acc9):
python bot_multi.py

# Or specify custom accounts:
python bot_multi.py --accounts acc1,acc2,acc3
```
* Monitors eligibility metrics per account.
* Auto-joins Mainnet Waitlist (Badge: First in Line).
* Places two-sided liquidity quotes in the orderbook (Badge: Keeper).
* Votes on active Meta Census questions (100% free gasless).
* Automatically selects high-probability markets.
* Prevents balance depletion via dynamic bankroll management.

### 2. View Eligibility Dashboard:
```bash
python -c "from eligibility import EligibilityTracker; [EligibilityTracker(a).print_dashboard() for a in ['acc1','acc2']]"
```

### 3. Single Account Mode:
```bash
python bot.py --mode growth
```
