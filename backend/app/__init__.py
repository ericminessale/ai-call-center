import os
from flask import Flask, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_socketio import SocketIO
from flask_bcrypt import Bcrypt
from flask_jwt_extended import JWTManager
import redis

db = SQLAlchemy()
migrate = Migrate()
socketio = SocketIO(async_mode='threading', logger=True, engineio_logger=False)
bcrypt = Bcrypt()
jwt = JWTManager()
redis_client = None
_app_instance = None


def create_app_context():
    """Return the Flask app context for use in background threads."""
    if _app_instance is None:
        raise RuntimeError("App not initialized yet")
    return _app_instance.app_context()


def _require_env(key: str) -> str:
    """Read a required env var or fail loudly. Beats silently running with a
    well-known default secret in production."""
    val = os.getenv(key)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {key}. "
            f"Set it in your .env (generate with: "
            f"python -c 'import secrets; print(secrets.token_urlsafe(32))')"
        )
    return val


def _cors_origins() -> list[str]:
    """Parse CORS_ORIGINS from env (comma-separated). Defaults to localhost
    for dev. ``*`` is rejected because it's incompatible with
    ``supports_credentials=True`` per the CORS spec."""
    raw = os.getenv(
        'CORS_ORIGINS',
        'http://localhost,http://localhost:3000,http://localhost:3100,http://localhost:5173',
    )
    origins = [o.strip() for o in raw.split(',') if o.strip()]
    if '*' in origins:
        raise RuntimeError(
            "CORS_ORIGINS cannot contain '*' when credentials are enabled. "
            "List specific origins instead (browsers reject wildcard+credentials)."
        )
    return origins


def create_app():
    global _app_instance
    app = Flask(__name__)
    _app_instance = app

    # SEC-07: honor X-Forwarded-For/-Proto from a known number of trusted
    # reverse-proxy hops (e.g. 1 for kamal-proxy/nginx, 2 with Cloudflare in
    # front). Fixes request.is_secure behind TLS termination so the demo
    # session cookie gets its Secure flag, and makes request.remote_addr the
    # real client IP for rate limiting. Default 0 (off) preserves the
    # clone-and-own direct-exposure behavior; deliberately NOT trusting
    # X-Forwarded-Host (see the SEC-05 note below — EXTERNAL_URL is the only
    # trusted host source).
    try:
        trusted_proxies = int(os.getenv('TRUSTED_PROXY_COUNT', '0').strip() or '0')
    except ValueError:
        trusted_proxies = 0
    if trusted_proxies > 0:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=trusted_proxies,
            x_proto=trusted_proxies,
            x_host=0,
            x_port=0,
        )

    # Configuration — secrets are required, no fallback to a known string.
    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = _require_env('SECRET_KEY')
    app.config['JWT_SECRET_KEY'] = _require_env('JWT_SECRET_KEY')

    # Webhook/internal auth credentials — secure-by-default. SignalWire-facing
    # webhooks and the private backend⇄ai-agents routes authenticate via HTTP
    # Basic (see app/utils/webhook_auth.py). Require the creds at boot unless
    # the operator has explicitly opted into soft-mode for a migration window
    # (WEBHOOK_AUTH_REQUIRED=false). The /api/internal/* routes always enforce
    # regardless of that flag, so missing creds there fail loud at request time.
    if os.getenv('WEBHOOK_AUTH_REQUIRED', 'true').strip().lower() != 'false':
        _require_env('WEBHOOK_AUTH_USER')
        _require_env('WEBHOOK_AUTH_PASSWORD')

    # SEC-05 fix (2026-06-02 audit): EXTERNAL_URL is the only trustworthy
    # source for callback URLs handed to SignalWire. The previous
    # X-Forwarded-Host fallback in url_utils.get_base_url was attacker-
    # controllable on an inbound request — an attacker who could hit a
    # webhook endpoint could poison the callback host SignalWire receives
    # for subsequent legs of the call, redirecting them. Fail fast at
    # boot so an operator can never accidentally launch without it.
    _require_env('EXTERNAL_URL')

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)

    cors_origins = _cors_origins()
    CORS(app, resources={r"/*": {"origins": cors_origins}}, supports_credentials=True)
    app.logger.info("CORS enabled for origins: %s", cors_origins)

    redis_url = os.getenv('REDIS_URL', 'redis://redis:6379/0')
    socketio.init_app(app,
                     cors_allowed_origins=cors_origins,
                     message_queue=redis_url,
                     async_mode='threading',
                     ping_timeout=60,
                     ping_interval=25)
    bcrypt.init_app(app)
    jwt.init_app(app)

    # SEC-03 closure for the flask-jwt-extended path: @jwt_required()
    # routes (Call Fabric token mint, contacts, …) don't go through our
    # custom verify_token, so without this loader a demo persona whose
    # lease was released or TTL-expired could still mint live subscriber
    # tokens. The blocklist loader runs on every @jwt_required() request
    # and treats a stale persona token as revoked (401).
    @jwt.token_in_blocklist_loader
    def _persona_token_revoked(_jwt_header, jwt_payload):
        from app.utils.jwt_utils import persona_claims_are_stale
        return persona_claims_are_stale(jwt_payload)

    # Initialize Redis with connection pooling and retries
    global redis_client
    from redis.connection import ConnectionPool

    # Create connection pool with robust settings
    pool = ConnectionPool.from_url(
        os.getenv('REDIS_URL', 'redis://redis:6379/0'),
        decode_responses=True,
        socket_timeout=10,
        socket_connect_timeout=10,
        socket_keepalive=True,
        retry_on_timeout=True,
        health_check_interval=30,
        max_connections=50
    )

    redis_client = redis.Redis(connection_pool=pool)

    # Test connection
    try:
        redis_client.ping()
        app.logger.info(f"Redis connected successfully")
    except Exception as e:
        app.logger.warning(f"Initial Redis connection failed: {str(e)} - will retry on demand")

    # Register blueprints
    from app.api import auth_bp, calls_bp, swml_bp, webhooks_bp, admin_bp, contacts_bp, conferences_bp, call_control_bp
    from app.api.queues import queues_bp
    from app.api.fabric import fabric_bp
    from app.api.ai_control import ai_control_bp
    from app.api.internal import internal_bp
    from app.api.demo import demo_bp
    from app.api.callbacks import callbacks_bp
    app.register_blueprint(auth_bp, url_prefix='/api/auth')
    app.register_blueprint(calls_bp, url_prefix='/api/calls')
    app.register_blueprint(contacts_bp, url_prefix='/api/contacts')
    app.register_blueprint(swml_bp, url_prefix='/api/swml')
    app.register_blueprint(webhooks_bp, url_prefix='/api/webhooks')
    app.register_blueprint(queues_bp, url_prefix='/api/queues')
    app.register_blueprint(fabric_bp, url_prefix='/api/fabric')
    app.register_blueprint(ai_control_bp, url_prefix='/api/ai')
    app.register_blueprint(admin_bp, url_prefix='/api/admin')
    app.register_blueprint(conferences_bp, url_prefix='/api/conferences')
    app.register_blueprint(call_control_bp, url_prefix='/api/call-control')
    app.register_blueprint(internal_bp, url_prefix='/api/internal')
    app.register_blueprint(callbacks_bp, url_prefix='/api/callbacks')
    # Demo blueprint mounts at /api (so config/runtime + demo/start sit
    # cleanly under their respective paths); it self-gates on DEMO_MODE
    # internally — registering it on every deployment is safe.
    app.register_blueprint(demo_bp, url_prefix='/api')

    # Initialize tap audio relay WebSocket routes
    from app.services.tap_relay import init_tap_relay
    init_tap_relay(app)

    # Import WebSocket handlers (must be after socketio.init_app)
    with app.app_context():
        from app.services import socketio_events  # Basic connection handlers
        from app.services import callcenter_socketio  # Call center specific handlers

    # Long-lived background boot tasks (queue monitor, fabric sync, stale-call
    # watchdog, demo-persona seed) must NOT run when the app object is loaded
    # for a short-lived CLI command like `flask db upgrade` — the process
    # exits immediately, but any Redis singleton lock it grabbed would linger
    # and make the real gunicorn workers skip the task (leaving e.g. the
    # watchdog unstarted). entrypoint.sh sets SKIP_BOOT_TASKS=1 for the
    # migrate/seed phase; the gunicorn exec runs without it.
    _skip_boot_tasks = os.getenv('SKIP_BOOT_TASKS', '').strip() == '1'

    if not _skip_boot_tasks:
        with app.app_context():
            # Start queue monitor after imports
            callcenter_socketio.start_queue_monitor()

    # Health check route
    @app.route('/health')
    def health():
        return {'status': 'healthy'}

    # Sync managed Fabric webhook URLs to current EXTERNAL_URL. Idempotent —
    # a no-op if URLs already match. Keeps the agent-conference-swml resource
    # pointing at the live tunnel after rotation, without manual Dashboard edits.
    #
    # DEPLOY-H4: this hits the SignalWire REST API and used to run in every
    # gunicorn worker (4x per boot). Gate it behind a Redis SET NX lock so only
    # the first worker performs the boot sync; the rest skip it. Also skipped
    # entirely during the CLI/migrate phase (_skip_boot_tasks).
    if not _skip_boot_tasks:
        try:
            _do_fabric_sync = True
            try:
                from app.services.redis_service import get_redis_client
                _rc = get_redis_client()
                if _rc is not None:
                    _do_fabric_sync = bool(
                        _rc.set('fabric_sync_boot_lock', '1', nx=True, ex=120)
                    )
            except Exception:
                _do_fabric_sync = True  # Redis unavailable — better to sync than skip.
            if _do_fabric_sync:
                from app.services.fabric_sync import sync_all
                result = sync_all(os.getenv('EXTERNAL_URL', ''))
                # Warning level so it surfaces in gunicorn's default log config.
                app.logger.warning(f"[fabric_sync] startup sync: {result}")
            else:
                app.logger.info("[fabric_sync] startup sync already done by another worker — skipping")
        except Exception as e:
            app.logger.warning(f"[fabric_sync] startup sync failed (non-fatal): {e}")

    # Start the stale-call watchdog. Background greenlet that reaps Call rows
    # whose SWML heartbeat key has expired in Redis — our only reliable signal
    # that a parked caller has dropped (see app/services/call_watchdog.py for
    # full rationale).
    if not _skip_boot_tasks:
        try:
            from app.services.call_watchdog import start as start_call_watchdog
            start_call_watchdog(app)
        except Exception as e:
            app.logger.error(f"[call_watchdog] start failed (non-fatal): {e}")

    # Top up the demo-persona pool when DEMO_MODE is on. Idempotent —
    # no-op on production-shape clone-and-own deployments.
    if not _skip_boot_tasks:
        try:
            from app.utils.demo_config import is_demo_mode
            if is_demo_mode():
                with app.app_context():
                    from app.services.demo_seed import seed_demo_personas
                    seed_result = seed_demo_personas()
                    app.logger.warning(f"[demo_seed] startup: {seed_result}")
        except Exception as e:
            # Don't crash the app for a seed failure — log loudly so it gets fixed.
            app.logger.error(f"[demo_seed] startup failed (non-fatal): {e}")

    return app