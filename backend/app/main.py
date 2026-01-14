import os
import sys

# Ensure the app module can be imported
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from __init__ import create_app, socketio, db

app = create_app()

# Ensure app context is available for workers
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    port = int(os.environ.get('APP_PORT', 5000))
    # Use eventlet without manual monkey patching
    # The eventlet worker will handle this properly
    socketio.run(app, debug=False, host='0.0.0.0', port=port)