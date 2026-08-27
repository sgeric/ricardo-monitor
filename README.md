# Ricardo.ch hourly photo/video monitor

This project checks your selected Ricardo.ch searches once per hour and emails
only newly discovered listings offering:

- **Sofort kaufen** (Buy Now), or
- **Sofort kaufen + Preis vorschlagen** (Buy Now + Price Suggestion)

It does not log in to Ricardo and never bids or buys anything.

## Watch list

- Sony a6000 / a6100 / a6300 / a6400 / a6500 / a6600 / a6700
- Sony a7 II / III / IV
- Samyang lenses for Sony
- Sigma lenses for Sony E / FE
- DJI Air 2 / 2S / 3 / 3S
- DJI Mini 3 / 4 / 5 families, including Pro
- DJI RS gimbals
- DJI Osmo Action 3 / 4 / 5 / 6
- DJI Pocket / Osmo Pocket
- Godox flashes and lighting
- RØDE VideoMic and Wireless microphones

## Easiest deployment: GitHub Actions

1. Create a new **private** GitHub repository.
2. Upload all files from this folder, including `.github/workflows/ricardo.yml`.
3. Go to **Settings → Secrets and variables → Actions → New repository secret**.
4. Add:

   - `ALERT_EMAIL` = the email address that should receive alerts
   - `SMTP_HOST`
   - `SMTP_PORT` = `587`
   - `SMTP_USERNAME`
   - `SMTP_PASSWORD`
   - `SMTP_FROM` = optional; normally same as SMTP_USERNAME

### If you send through Gmail

Use:
- `SMTP_HOST` = `smtp.gmail.com`
- `SMTP_PORT` = `587`
- `SMTP_USERNAME` = your Gmail address
- `SMTP_PASSWORD` = a Google **App Password**, not your normal account password

You may send the alert to any recipient address, including Yahoo.

### If you send through Yahoo Mail

Use:
- `SMTP_HOST` = `smtp.mail.yahoo.com`
- `SMTP_PORT` = `587`
- `SMTP_USERNAME` = your Yahoo email address
- `SMTP_PASSWORD` = a Yahoo **App Password**

Do not put passwords directly into `monitor.py`. Keep them as GitHub Secrets.

## First run

The workflow currently uses:

    INITIAL_MODE: notify

That means the first run will email every currently visible matching Buy Now
listing found by the monitor. After that, it only emails genuinely new article IDs.

If you want the first run to be silent and merely establish a baseline, change:

    INITIAL_MODE: baseline

Then run it once, and switch it back to `notify`.

## About the requested "starting yesterday" baseline

Ricardo's public search/listing HTML does not reliably expose a trustworthy
publication timestamp for every listing. Because of that, this monitor does not
pretend it can determine with certainty which currently active listing was first
published yesterday.

The reliable boundary begins once `seen.json` is created: from then onward,
new Ricardo article IDs are detected deterministically.

If you already checked all older ads, leave `INITIAL_MODE: notify`. Review the
first email once; all those IDs are then remembered and future alerts are clean.

## Local test

Install:

    pip install -r requirements.txt

Set the SMTP environment variables and run:

    python monitor.py

## Notes

Ricardo may change its public HTML or anti-bot protections. If the parser stops
finding listings, the script may need a small update.
