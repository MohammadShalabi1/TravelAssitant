from google import genai


def create_gemini_client(api_key: str) -> genai.Client:
    return genai.Client(api_key=api_key)
