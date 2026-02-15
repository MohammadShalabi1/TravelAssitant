conversation_history = []

def add_message(msg):
    conversation_history.append(msg)

def get_history():
    return "\n".join(conversation_history[-5:])
