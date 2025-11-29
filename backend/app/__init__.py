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

def create_app():
    app = Flask(__name__)

    # Configuration
    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret')
    app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET_KEY', 'jwt-secret-key')

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)
    socketio.init_app(app,
                     cors_allowed_origins="*",
                     async_mode='threading',
                     ping_timeout=60,
                     ping_interval=25)
    bcrypt.init_app(app)
    jwt.init_app(app)

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
    from app.api import auth_bp, calls_bp, swml_bp, webhooks_bp, admin_bp
    from app.api.queues import queues_bp
    from app.api.fabric import fabric_bp
    from app.api.ai_control import ai_control_bp
    app.register_blueprint(auth_bp, url_prefix='/api/auth')
    app.register_blueprint(calls_bp, url_prefix='/api/calls')
    app.register_blueprint(swml_bp, url_prefix='/api/swml')
    app.register_blueprint(webhooks_bp, url_prefix='/api/webhooks')
    app.register_blueprint(queues_bp, url_prefix='/api/queues')
    app.register_blueprint(fabric_bp, url_prefix='/api/fabric')
    app.register_blueprint(ai_control_bp, url_prefix='/api/ai')
    app.register_blueprint(admin_bp, url_prefix='/api/admin')

    # Import WebSocket handlers (must be after socketio.init_app)
    with app.app_context():
        from app.services import socketio_events  # Basic connection handlers
        from app.services import callcenter_socketio  # Call center specific handlers

        # Start queue monitor after imports
        callcenter_socketio.start_queue_monitor()

    # Request logging disabled to reduce spam
    # @app.before_request
    # def log_request():
    #     if request.path.startswith('/api/'):
    #         print(f"🌐 [REQUEST] {request.method} {request.path}")
    #         print(f"🌐 [REQUEST] Headers: {dict(request.headers)}")
    #         if request.is_json:
    #             print(f"🌐 [REQUEST] Body: {request.get_json()}")

    # @app.after_request
    # def log_response(response):
    #     if request.path.startswith('/api/'):
    #         print(f"🌐 [RESPONSE] {request.method} {request.path} -> {response.status_code}")
    #     return response

    # Health check route
    @app.route('/health')
    def health():
        return {'status': 'healthy'}

    return app