from argparse import ArgumentParser
import requests
import json
import time

if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--address", type=str, default="http://localhost:8001/prompt")
    args = parser.parse_args()

    user_messages = [
#        'hi',
#         'what can you do?',
#         'search for latest research on AI',
#         'search for latest research on AI then computer vision',
#         'then summarize the results'
    ]

    user_messages_it = iter(user_messages)
    messages = []
    
    address = args.address
    
    while True:
        message = next(user_messages_it, None)
        
        if callable(message):
            message = message()

        if message is None:
            message = input("\n\nType your message (or 'exit' to quit): ")


        print("\n" * 2)
        print("User: ", message, end="", flush=True)

        messages.append({"role": "user", "content": message})
        response = requests.post(
            address, 
            json={
                "messages": messages, 
                "id": str(time.time()),
                "stream": True
                },
            stream=True
        )

        with open("chat_history.json", "w") as f:
            json.dump(messages, f, indent=2)

        print("\n" * 2)
        print("Assistant: ", end="", flush=True)
        
        assistant_message = ''

        for chunk in response.iter_lines():
            if chunk and chunk.startswith(b"data: "):
                chunk = chunk[6:]
        
                if chunk == b"[DONE]":
                    break

                
                try:
                    decoded_chunk = chunk.decode("utf-8")
                    json_chunk = json.loads(decoded_chunk)
                    content = json_chunk["choices"][0]["delta"]["content"]
                    role = json_chunk["choices"][0]["delta"]["role"]

                    print(content, end="", flush=True)

                    if role in [None, "assistant"]:
                        assistant_message += content

                except Exception as e:
                    print(f"Error: {e}")
                    print(f"Chunk: {chunk}")

        messages.append({"role": "assistant", "content": assistant_message})
        print("\n" * 2)
