from signalwire_agents import AgentBase

# Create a test agent to see what methods are available
class TestAgent(AgentBase):
    def __init__(self):
        super().__init__(name='TestAgent', route='/test')

        # Print all available methods
        print("Available methods in AgentBase:")
        for attr in dir(self):
            if not attr.startswith('_'):
                print(f"  - {attr}")

if __name__ == '__main__':
    test = TestAgent()