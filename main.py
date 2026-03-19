from dotenv import load_dotenv
from google import genai
import os

from agent.loop import run_agentic_loop

load_dotenv()
gemini_key = os.getenv("GEMINI_API_KEY")

def main():
    client = genai.Client(api_key='//////')
    run_agentic_loop(client)

if __name__ == "__main__":
    main()
