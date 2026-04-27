from flask import Blueprint

# Create blueprints
auth_bp = Blueprint('auth', __name__)
calls_bp = Blueprint('calls', __name__)
swml_bp = Blueprint('swml', __name__)
webhooks_bp = Blueprint('webhooks', __name__)
admin_bp = Blueprint('admin', __name__)

# Import routes after blueprint creation to avoid circular imports
from app.api import auth, calls, swml, webhooks, admin
# mcp_gateways registers its routes onto admin_bp; import after admin so
# the admin module's @admin_bp.before_request auth gate is in place.
from app.api import mcp_gateways  # noqa: F401

# Import blueprints defined in their own modules
from app.api.contacts import contacts_bp
from app.api.conferences import conferences_bp
from app.api.call_control import call_control_bp