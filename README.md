# Telecom USSD Flexiload API

Django REST Framework backend for authenticated mobile top-ups through a
serial/USB SIM modem. It includes token authentication, encrypted per-user SIM
PINs, operator-specific USSD generation, transaction history, admin access,
idempotency protection, MySQL support, and rotating transaction logs.

## Safety first

The project starts with `MODEM_BACKEND=mock`, so development requests do not
dial real USSD codes. Verify every operator's current retailer command with the
carrier before switching to `serial`. USSD menus and modem AT behavior can vary.
The Banglalink input notation `#,0` is treated as a dial suffix/mode indicator;
the actual USSD payload ends at `#`, and the modem DCS is supplied separately.

Never log, print, or expose a rendered USSD command because it contains the PIN.
Changing `PIN_ENCRYPTION_KEY` without re-encrypting existing values makes stored
PINs unreadable.

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py generate_pin_key
```

Copy `.env.example` to `.env`, set the generated key, and load those environment
variables in your service manager or shell. Django does not parse `.env` files
automatically.

For a quick SQLite development run:

```powershell
$env:PIN_ENCRYPTION_KEY="your-generated-key"
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Admin: `http://127.0.0.1:8000/admin/`

Operator dashboard: `http://127.0.0.1:8000/dashboard/`

The custom dashboard is the primary interface for daily operation. It provides
modem scanning and connection, SIM verification, top-up submission, searchable
transaction history, complete modem response display, and API documentation.
The Django admin remains available only for low-level maintenance.

### Admin modem control

Open **SIM profiles** in Django admin to use the unified modem control page:

- **Detect & Connect** scans Windows/Linux serial ports and identifies the
  AT-responsive modem channel, network operator, SIM state, signal, model, and
  IMEI. Scanning does not dial USSD.
- **Connected SIMs** shows saved profiles and lets an administrator edit them.
- **Top Up** accepts the connected SIM, product type, mobile number, amount, and
  an optional PIN override. It previews the USSD pattern with the PIN hidden and
  requires confirmation before dialing.
- **Transactions** shows the most recent top-up activity.

After scanning, enter the Flexiload PIN on the matching modem card and press
**Connect**. The PIN is encrypted immediately. Automatic operator detection
depends on the SIM being network-registered; if the modem reports no operator,
select it manually.

Connection now verifies the SIM by dialing its own-number code and saving the
returned number:

- Grameenphone: `*2#`
- Banglalink: `*511#`
- Airtel: `*282#`
- Robi: `*140*2*4#`

The same verification runs immediately before every top-up. A missing, swapped,
busy, or wrong-port SIM causes the transaction to fail before the Flexiload
USSD command is sent.

## API

All protected requests use:

```text
Authorization: Token <token>
Content-Type: application/json
```

### Login

`POST /api/auth/login/`

```json
{"email": "agent@example.com", "password": "secret"}
```

Other auth endpoints:

- `POST /api/auth/logout/`
- `GET /api/auth/me/`

### Create a SIM profile

`POST /api/sim-profiles/`

```json
{
  "name": "Counter GP SIM",
  "operator": "grameenphone",
  "port": "COM3",
  "pin": "1234",
  "is_active": true
}
```

The PIN is write-only and encrypted at rest.

### Send a top-up

`POST /api/transactions/send/`

```json
{
  "sim_profile_id": 1,
  "product": "flexiload",
  "phone_number": "01712345678",
  "amount": "50.00",
  "idempotency_key": "order-2026-0001"
}
```

An optional write-only `pin` replaces the SIM profile's saved PIN before use.
Use a unique `idempotency_key` for every business order to prevent accidental
double submission.

History:

- `GET /api/transactions/`
- `GET /api/transactions/{uuid}/`

Normal users only see their own SIM profiles and transactions. Staff users can
inspect all records.

## Operators and products

- Grameenphone: `flexiload`, `offer`, `postpaid`, `prepaid`, `regular`, `skitto`, `topup`
- Robi: `flexiload`, `offer`, `postpaid`, `prepaid`, `regular`, `topup`
- Airtel: `flexiload`, `offer`, `postpaid`, `prepaid`, `regular`, `topup`
- Banglalink: `flexiload`, `offer`, `postpaid`, `prepaid`, `regular`, `topup`

## Real modem mode

Install `pyserial`, connect an AT-command-compatible modem, and set:

```text
MODEM_BACKEND=serial
MODEM_PORT=COM3
MODEM_BAUDRATE=115200
MODEM_TIMEOUT=30
```

Linux commonly uses `/dev/ttyUSB0` or `/dev/ttyACM0`. The service account needs
permission to access that device. The driver sends `AT`, enables verbose modem
errors, and sends USSD with `AT+CUSD`.

## MySQL / Docker

Set the variables referenced by `docker-compose.yml`, then run:

```bash
docker compose up --build
docker compose exec api python manage.py createsuperuser
```

For a modem in Docker on Linux, set `MODEM_DEVICE=/dev/ttyUSB0`. Direct serial
device passthrough differs on Docker Desktop for Windows; running the API on the
Windows host is usually simpler.

## Verification

```powershell
python manage.py check
python manage.py makemigrations --check
python manage.py test
```
project run 


active venv and one terminal run:
python manage.py poll_modem_messages --interval 20


another terminal run: active venv and
.\venv\Scripts\Activate.ps1
$env:MODEM_BACKEND="serial"
python manage.py runserver 0.0.0.0:8080      
