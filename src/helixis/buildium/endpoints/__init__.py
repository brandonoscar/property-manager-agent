"""Resource-grouped endpoint wrappers over BuildiumClient."""

from helixis.buildium.endpoints.administration import Administration
from helixis.buildium.endpoints.applicants import Applicants
from helixis.buildium.endpoints.bank_accounts import BankAccounts
from helixis.buildium.endpoints.bills import Bills
from helixis.buildium.endpoints.communications import Communications
from helixis.buildium.endpoints.files import Files
from helixis.buildium.endpoints.general_ledger import GeneralLedger
from helixis.buildium.endpoints.leases import Leases
from helixis.buildium.endpoints.listings import Listings
from helixis.buildium.endpoints.meter_readings import MeterReadings
from helixis.buildium.endpoints.rentals import Rentals
from helixis.buildium.endpoints.requests import Requests
from helixis.buildium.endpoints.tasks import Tasks
from helixis.buildium.endpoints.vendors import Vendors
from helixis.buildium.endpoints.work_orders import WorkOrders

__all__ = [
    "Administration",
    "Applicants",
    "BankAccounts",
    "Bills",
    "Communications",
    "Files",
    "GeneralLedger",
    "Leases",
    "Listings",
    "MeterReadings",
    "Rentals",
    "Requests",
    "Tasks",
    "Vendors",
    "WorkOrders",
]
