"""SignalWire REST API client for making calls."""
import os
import logging
import requests
import base64
import json
from flask import current_app

logger = logging.getLogger(__name__)


class SignalWireAPI:
    """SignalWire REST API client."""

    def __init__(self):
        """Initialize SignalWire API client."""
        self.space = current_app.config.get('SIGNALWIRE_SPACE') or os.environ.get('SIGNALWIRE_SPACE')
        self.project_id = current_app.config.get('SIGNALWIRE_PROJECT_ID') or os.environ.get('SIGNALWIRE_PROJECT_ID')
        self.api_token = current_app.config.get('SIGNALWIRE_API_TOKEN') or os.environ.get('SIGNALWIRE_API_TOKEN')
        self.from_number = current_app.config.get('SIGNALWIRE_FROM_NUMBER') or os.environ.get('SIGNALWIRE_FROM_NUMBER', '+12068655443')

        if not all([self.space, self.project_id, self.api_token]):
            raise ValueError("SignalWire credentials not properly configured")

        # Create auth header
        auth_string = f"{self.project_id}:{self.api_token}"
        self.auth_header = f"Basic {base64.b64encode(auth_string.encode()).decode()}"

        # API endpoint
        self.api_url = f"https://{self.space}/api/calling/calls"

    def create_call(self, to, swml_url, status_callback=None):
        """Create an outbound call using SignalWire API."""
        try:
            headers = {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'Authorization': self.auth_header
            }

            # Build the call parameters
            params = {
                "from": self.from_number,
                "to": to,
                "url": swml_url
            }

            # Add call state events if callback provided
            if status_callback:
                params["call_state_events"] = ["created", "ringing", "answered", "ended"]
                params["call_state_url"] = status_callback

            data = {
                "command": "dial",
                "params": params
            }

            # Log the outgoing request
            logger.info("="*50)
            logger.info(f"SIGNALWIRE API REQUEST: POST {self.api_url}")
            logger.info(f"Headers: {json.dumps({k: v if k != 'Authorization' else 'Bearer ***' for k, v in headers.items()}, indent=2)}")
            logger.info(f"JSON BODY: {json.dumps(data, indent=2)}")
            logger.info("="*50)

            response = requests.post(
                self.api_url,
                json=data,
                headers=headers
            )

            # Log the response
            logger.info(f"SIGNALWIRE API RESPONSE: Status {response.status_code}")
            if response.text:
                try:
                    logger.info(f"JSON RESPONSE: {json.dumps(response.json(), indent=2)}")
                except:
                    logger.info(f"RAW RESPONSE: {response.text}")
            logger.info("="*50)

            if response.status_code >= 200 and response.status_code < 300:
                result = response.json()
                call_id = result.get('call_id', result.get('id'))
                logger.info(f"Call created successfully: {call_id} to {to}")

                # Return an object similar to what the old client returns
                return type('Call', (), {
                    'sid': call_id,
                    'to': to,
                    'from_': self.from_number,
                    'status': 'initiated',
                    'raw_response': result
                })()
            else:
                logger.error(f"Failed to create call: {response.status_code} - {response.text}")
                raise Exception(f"Failed to create call: {response.status_code}")

        except Exception as e:
            logger.error(f"Failed to create call: {str(e)}")
            raise

    def update_call(self, call_sid, swml_url):
        """Update an active call with new SWML."""
        try:
            headers = {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'Authorization': self.auth_header
            }

            data = {
                "command": "update",
                "params": {
                    "call_id": call_sid,
                    "url": swml_url
                }
            }

            # Log the outgoing request
            logger.info("="*50)
            logger.info(f"SIGNALWIRE API REQUEST: POST {self.api_url}")
            logger.info(f"Headers: {json.dumps({k: v if k != 'Authorization' else 'Bearer ***' for k, v in headers.items()}, indent=2)}")
            logger.info(f"JSON BODY: {json.dumps(data, indent=2)}")
            logger.info("="*50)

            response = requests.post(
                self.api_url,
                json=data,
                headers=headers
            )

            # Log the response
            logger.info(f"SIGNALWIRE API RESPONSE: Status {response.status_code}")
            if response.text:
                try:
                    logger.info(f"JSON RESPONSE: {json.dumps(response.json(), indent=2)}")
                except:
                    logger.info(f"RAW RESPONSE: {response.text}")
            logger.info("="*50)

            if response.status_code >= 200 and response.status_code < 300:
                logger.info(f"Call updated successfully: {call_sid}")
                return response.json()
            else:
                logger.error(f"Failed to update call: {response.status_code} - {response.text}")
                raise Exception(f"Failed to update call: {response.status_code}")

        except Exception as e:
            logger.error(f"Failed to update call: {str(e)}")
            raise

    def start_transcription(self, call_id, webhook_url):
        """Start live transcription on a call."""
        try:
            headers = {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'Authorization': self.auth_header
            }

            data = {
                "id": call_id,
                "command": "calling.live_transcribe",
                "params": {
                    "action": {
                        "start": {
                            "webhook": webhook_url,
                            "lang": "en-US",
                            "live_events": True,
                            "ai_summary": True,
                            "direction": ["remote-caller"]
                        }
                    }
                }
            }

            # Log the outgoing request
            logger.info("="*50)
            logger.info(f"SIGNALWIRE API REQUEST: POST {self.api_url}")
            logger.info(f"Headers: {json.dumps({k: v if k != 'Authorization' else 'Bearer ***' for k, v in headers.items()}, indent=2)}")
            logger.info(f"JSON BODY: {json.dumps(data, indent=2)}")
            logger.info("="*50)

            response = requests.post(
                self.api_url,
                json=data,
                headers=headers
            )

            # Log the response
            logger.info(f"SIGNALWIRE API RESPONSE: Status {response.status_code}")
            if response.text:
                try:
                    logger.info(f"JSON RESPONSE: {json.dumps(response.json(), indent=2)}")
                except:
                    logger.info(f"RAW RESPONSE: {response.text}")
            logger.info("="*50)

            if response.status_code >= 200 and response.status_code < 300:
                logger.info(f"Transcription started successfully for call: {call_id}")
                return response.json()
            else:
                logger.error(f"Failed to start transcription: {response.status_code} - {response.text}")
                raise Exception(f"Failed to start transcription: {response.status_code}")

        except Exception as e:
            logger.error(f"Failed to start transcription: {str(e)}")
            raise

    def stop_transcription(self, call_id):
        """Stop live transcription on a call."""
        try:
            headers = {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'Authorization': self.auth_header
            }

            data = {
                "id": call_id,
                "command": "calling.live_transcribe",
                "params": {
                    "action": "stop"
                }
            }

            # Log the outgoing request
            logger.info("="*50)
            logger.info(f"SIGNALWIRE API REQUEST: POST {self.api_url}")
            logger.info(f"Headers: {json.dumps({k: v if k != 'Authorization' else 'Bearer ***' for k, v in headers.items()}, indent=2)}")
            logger.info(f"JSON BODY: {json.dumps(data, indent=2)}")
            logger.info("="*50)

            response = requests.post(
                self.api_url,
                json=data,
                headers=headers
            )

            # Log the response
            logger.info(f"SIGNALWIRE API RESPONSE: Status {response.status_code}")
            if response.text:
                try:
                    logger.info(f"JSON RESPONSE: {json.dumps(response.json(), indent=2)}")
                except:
                    logger.info(f"RAW RESPONSE: {response.text}")
            logger.info("="*50)

            if response.status_code >= 200 and response.status_code < 300:
                logger.info(f"Transcription stopped successfully for call: {call_id}")
                return response.json()
            else:
                logger.error(f"Failed to stop transcription: {response.status_code} - {response.text}")
                raise Exception(f"Failed to stop transcription: {response.status_code}")

        except Exception as e:
            logger.error(f"Failed to stop transcription: {str(e)}")
            raise

    def summarize_call(self, call_id, webhook_url, prompt=None):
        """Request AI summary of call transcription."""
        try:
            headers = {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'Authorization': self.auth_header
            }

            data = {
                "id": call_id,
                "command": "calling.live_transcribe",
                "params": {
                    "action": {
                        "summarize": {
                            "webhook": webhook_url,
                            "prompt": prompt or "Summarize the key points of this conversation."
                        }
                    }
                }
            }

            # Log the outgoing request
            logger.info("="*50)
            logger.info(f"SIGNALWIRE API REQUEST: POST {self.api_url}")
            logger.info(f"Headers: {json.dumps({k: v if k != 'Authorization' else 'Bearer ***' for k, v in headers.items()}, indent=2)}")
            logger.info(f"JSON BODY: {json.dumps(data, indent=2)}")
            logger.info("="*50)

            response = requests.post(
                self.api_url,
                json=data,
                headers=headers
            )

            # Log the response
            logger.info(f"SIGNALWIRE API RESPONSE: Status {response.status_code}")
            if response.text:
                try:
                    logger.info(f"JSON RESPONSE: {json.dumps(response.json(), indent=2)}")
                except:
                    logger.info(f"RAW RESPONSE: {response.text}")
            logger.info("="*50)

            if response.status_code >= 200 and response.status_code < 300:
                logger.info(f"Summary request sent successfully for call: {call_id}")
                return response.json()
            else:
                logger.error(f"Failed to request summary: {response.status_code} - {response.text}")
                raise Exception(f"Failed to request summary: {response.status_code}")

        except Exception as e:
            logger.error(f"Failed to request summary: {str(e)}")
            raise

    def send_ai_message(self, call_id, message_text, role="system"):
        """Send a system message to an active AI agent during a call.

        Args:
            call_id: The SignalWire call ID
            message_text: The message content to send to the AI
            role: Message role - typically "system" for interventions, can also be "user" or "assistant"
        """
        try:
            headers = {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'Authorization': self.auth_header
            }

            data = {
                "id": call_id,
                "command": "calling.ai_message",
                "params": {
                    "role": role,
                    "message_text": message_text
                }
            }

            # Log the outgoing request
            logger.info("="*50)
            logger.info(f"SIGNALWIRE AI MESSAGE REQUEST: POST {self.api_url}")
            logger.info(f"Call ID: {call_id}")
            logger.info(f"Role: {role}")
            logger.info(f"Message: {message_text}")
            logger.info(f"JSON BODY: {json.dumps(data, indent=2)}")
            logger.info("="*50)

            response = requests.post(
                self.api_url,
                json=data,
                headers=headers
            )

            # Log the response
            logger.info(f"SIGNALWIRE API RESPONSE: Status {response.status_code}")
            if response.text:
                try:
                    logger.info(f"JSON RESPONSE: {json.dumps(response.json(), indent=2)}")
                except:
                    logger.info(f"RAW RESPONSE: {response.text}")
            logger.info("="*50)

            if response.status_code >= 200 and response.status_code < 300:
                logger.info(f"AI message sent successfully to call: {call_id}")
                return response.json()
            else:
                logger.error(f"Failed to send AI message: {response.status_code} - {response.text}")
                raise Exception(f"Failed to send AI message: {response.status_code}")

        except Exception as e:
            logger.error(f"Failed to send AI message: {str(e)}")
            raise

    def end_call(self, call_id):
        """End an active call."""
        try:
            headers = {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'Authorization': self.auth_header
            }

            data = {
                "id": call_id,
                "command": "calling.end",
                "params": {
                    "reason": "hangup"
                }
            }

            # Log the outgoing request
            logger.info("="*50)
            logger.info(f"SIGNALWIRE API REQUEST: POST {self.api_url}")
            logger.info(f"Headers: {json.dumps({k: v if k != 'Authorization' else 'Bearer ***' for k, v in headers.items()}, indent=2)}")
            logger.info(f"JSON BODY: {json.dumps(data, indent=2)}")
            logger.info("="*50)

            response = requests.post(
                self.api_url,
                json=data,
                headers=headers
            )

            # Log the response
            logger.info(f"SIGNALWIRE API RESPONSE: Status {response.status_code}")
            if response.text:
                try:
                    logger.info(f"JSON RESPONSE: {json.dumps(response.json(), indent=2)}")
                except:
                    logger.info(f"RAW RESPONSE: {response.text}")
            logger.info("="*50)

            if response.status_code >= 200 and response.status_code < 300:
                logger.info(f"Call ended successfully: {call_id}")
                return response.json()
            else:
                logger.error(f"Failed to end call: {response.status_code} - {response.text}")
                raise Exception(f"Failed to end call: {response.status_code}")

        except Exception as e:
            logger.error(f"Failed to end call: {str(e)}")
            raise


# Singleton instance
_signalwire_api = None


def get_signalwire_api():
    """Get or create SignalWire API instance."""
    global _signalwire_api
    if _signalwire_api is None:
        _signalwire_api = SignalWireAPI()
    return _signalwire_api