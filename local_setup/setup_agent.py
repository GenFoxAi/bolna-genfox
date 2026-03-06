"""
setup_agent.py  –  Create or update the Bolna voice agent.

Usage:
  python setup_agent.py              # Create new agent and print its ID
  python setup_agent.py --id <id>    # Update an existing agent
  python setup_agent.py --list       # List all agents in Redis
  python setup_agent.py --call <id> --to +91XXXXXXXXXX  # Make a call
"""

import argparse
import json
import requests

BASE_URL = "http://localhost:5001"

AGENT_CONFIG = {
    "agent_config": {
        "agent_name": "Real Estate Voice Agent",
        "agent_type": "other",
        "tasks": [
            {
                "tools_config": {
                    "llm_agent": {
                        "agent_flow_type": "streaming",
                        "agent_type": "simple_llm_agent",
                        "llm_config": {
                            "model": "gemini/gemini-2.5-flash",
                            "max_tokens": 150,
                            "family": "openai",
                            "temperature": 0.3,
                            "request_json": False,
                            "stop": None,
                            "top_k": 0,
                            "top_p": 0.9,
                            "min_p": 0.1,
                            "frequency_penalty": 0.0,
                            "presence_penalty": 0.0,
                            "provider": "gemini",
                            "base_url": None,
                            "reasoning_effort": None,
                            "verbosity": None,
                            "use_responses_api": False,
                            "agent_flow_type": "streaming",
                            "extraction_details": None,
                            "summarization_details": None
                        }
                    },
                    # ── Synthesizer ─────────────────────────────────────────────────────
                    # stream: false  →  uses reliable HTTP POST (not WS) so audio always works.
                    # Switch to true only when WS reconnection is fully stable.
                    "synthesizer": {
                        "provider": "sarvam",
                        "provider_config": {
                            "voice_id": "anushka",
                            "language": "en-IN",
                            "voice": "anushka",
                            "model": "bulbul:v3",
                            "speed": 1.0
                        },
                        "stream": False,          # HTTP mode – reliable
                        "buffer_size": 100,
                        "audio_format": "pcm",    # PCM for Twilio telephony
                        "caching": True
                    },
                    # ── Transcriber ──────────────────────────────────────────────────────
                    "transcriber": {
                        "model": "saarika:v2.5",
                        "language": "en-IN",
                        "stream": True,
                        "sampling_rate": 16000,
                        "encoding": "linear16",
                        "endpointing": 400,
                        "keywords": None,
                        "task": "transcribe",
                        "provider": "sarvam"
                    },
                    "input": {
                        "provider": "twilio",
                        "format": "wav"
                    },
                    "output": {
                        "provider": "twilio",
                        "format": "wav"
                    },
                    "api_tools": None
                },
                "toolchain": {
                    "execution": "parallel",
                    "pipelines": [["transcriber", "llm", "synthesizer"]]
                },
                "task_type": "conversation",
                "task_config": {}
            }
        ],
        "agent_welcome_message": "Hello! This is your AI assistant. How can I help you today?"
    },
    "agent_prompts": {
        "task_1": {
            "system_prompt": (
                "You are a helpful, friendly voice assistant for a real estate company. "
                "Keep responses very concise (1-2 sentences max) since this is a phone call. "
                "Be warm, professional and helpful."
            )
        }
    }
}


def create_agent():
    r = requests.post(f"{BASE_URL}/agent", json=AGENT_CONFIG)
    r.raise_for_status()
    data = r.json()
    agent_id = data["agent_id"]
    print(f"✅ Agent created: {agent_id}")
    return agent_id


def update_agent(agent_id):
    r = requests.put(f"{BASE_URL}/agent/{agent_id}", json=AGENT_CONFIG)
    r.raise_for_status()
    print(f"✅ Agent updated: {agent_id}")


def list_agents():
    r = requests.get(f"{BASE_URL}/all")
    r.raise_for_status()
    agents = r.json().get("agents", [])
    if not agents:
        print("No agents found.")
        return
    for a in agents:
        aid = a["agent_id"]
        name = a.get("data", {}).get("agent_name", "?")
        print(f"  {aid}  —  {name}")


def make_call(agent_id, phone_number):
    payload = {"agent_id": agent_id, "recipient_phone_number": phone_number}
    r = requests.post(f"{BASE_URL}/call", json=payload)
    r.raise_for_status()
    print(f"✅ Call initiated to {phone_number} using agent {agent_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manage Bolna agents")
    parser.add_argument("--id", help="Agent ID to update")
    parser.add_argument("--list", action="store_true", help="List all agents")
    parser.add_argument("--call", help="Agent ID to use for a call")
    parser.add_argument("--to", help="Phone number to call (e.g. +91XXXXXXXXXX)")
    args = parser.parse_args()

    if args.list:
        list_agents()
    elif args.call:
        if not args.to:
            print("Error: --to <phone_number> is required for calls")
        else:
            make_call(args.call, args.to)
    elif args.id:
        update_agent(args.id)
    else:
        agent_id = create_agent()
        print(f"\nTo make a call:")
        print(f"  python setup_agent.py --call {agent_id} --to +91XXXXXXXXXX")
