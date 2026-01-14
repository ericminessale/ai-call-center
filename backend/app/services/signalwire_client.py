from twilio.rest import Client
from flask import current_app
import logging

logger = logging.getLogger(__name__)


class SignalWireClient:
    """SignalWire client wrapper for making API calls."""

    def __init__(self):
        self.client = None
        self._initialize_client()

    def _initialize_client(self):
        """Initialize the SignalWire/Twilio client."""
        try:
            space = current_app.config.get('SIGNALWIRE_SPACE')
            project_id = current_app.config.get('SIGNALWIRE_PROJECT_ID')
            api_token = current_app.config.get('SIGNALWIRE_API_TOKEN')

            if not all([space, project_id, api_token]):
                raise ValueError("SignalWire credentials not configured")

            # Initialize Twilio client with SignalWire config
            self.client = Client(
                project_id,
                api_token,
                signalwire_space_url=f'{space}.signalwire.com'
            )
        except Exception as e:
            logger.error(f"Failed to initialize SignalWire client: {str(e)}")
            raise

    def create_call(self, to, swml_url, status_callback=None):
        """Create a new outbound call."""
        try:
            from_number = current_app.config.get('SIGNALWIRE_FROM_NUMBER')
            base_url = current_app.config.get('BASE_URL')

            if not from_number:
                raise ValueError("SIGNALWIRE_FROM_NUMBER not configured")

            call_params = {
                'url': swml_url,
                'to': to,
                'from_': from_number,
                'method': 'POST'
            }

            if status_callback:
                call_params.update({
                    'status_callback': status_callback,
                    'status_callback_method': 'POST',
                    'status_callback_event': ['initiated', 'ringing', 'answered', 'completed']
                })

            call = self.client.calls.create(**call_params)

            logger.info(f"Call created successfully: {call.sid}")
            return call

        except Exception as e:
            logger.error(f"Failed to create call: {str(e)}")
            raise

    def update_call(self, call_sid, swml_url):
        """Update an existing call with new SWML instructions."""
        try:
            call = self.client.calls(call_sid).update(
                url=swml_url,
                method='POST'
            )

            logger.info(f"Call updated successfully: {call_sid}")
            return call

        except Exception as e:
            logger.error(f"Failed to update call {call_sid}: {str(e)}")
            raise

    def end_call(self, call_sid):
        """End an active call."""
        try:
            call = self.client.calls(call_sid).update(status='completed')
            logger.info(f"Call ended successfully: {call_sid}")
            return call

        except Exception as e:
            logger.error(f"Failed to end call {call_sid}: {str(e)}")
            raise

    def get_call_status(self, call_sid):
        """Get the current status of a call."""
        try:
            call = self.client.calls(call_sid).fetch()
            return {
                'sid': call.sid,
                'status': call.status,
                'direction': call.direction,
                'duration': call.duration,
                'from': call.from_,
                'to': call.to,
                'start_time': call.start_time,
                'end_time': call.end_time
            }

        except Exception as e:
            logger.error(f"Failed to get call status for {call_sid}: {str(e)}")
            raise


# Singleton instance
_signalwire_client = None


def get_signalwire_client():
    """Get or create SignalWire client instance."""
    global _signalwire_client
    if _signalwire_client is None:
        _signalwire_client = SignalWireClient()
    return _signalwire_client