# HS Property Management Platform — Flask App

## Project Structure

```
hs_property/
├── app.py                     # Main Flask app (routes + mock data)
├── requirements.txt
└── templates/
    ├── base.html              # Master layout (sidebar, topbar, styles)
    ├── login.html             # Standalone login page
    ├── dashboard.html         # Main dashboard with charts
    ├── properties.html        # Property & unit management
    ├── tenants.html           # Tenant listing & search
    ├── invoices.html          # Invoices + Paystack payment modal
    ├── service.html           # Service requests & technician assignment
    ├── finance.html           # Finance reconciliation + charts
    └── settings.html          # Profile, notifications, Paystack config
```

## Setup & Run

```bash
# 1. Create virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the app
python app.py
```

Then open http://localhost:5000

## Demo Login Credentials

| Role     | Email              | Password  |
|----------|--------------------|-----------|
| Admin    | admin@hs.com       | admin123  |
| Landlord | landlord@hs.com    | land123   |
| Tenant   | tenant@hs.com      | tenant123 |

## Role-Based Access

- **Admin** — Full access to all pages
- **Landlord** — Properties, Tenants, Invoices, Finance, Service, Settings
- **Tenant** — My Invoices, Report Issue, Settings only

## Color Palette

- Crimson Red: `#C9184A`
- Dark Navy: `#0D3A6B`
- Background: `#FFFFFF` / `#F6F8FB`
- Sidebar: `#061524`
