"""
Quick diagnostic script to test connection to OpsHero backend.
Run: python test_connection.py
"""

import httpx
import sys

API_URL = "https://api.opshero.me"

def test_connection():
    print(f"Testing connection to: {API_URL}")
    print("-" * 60)
    
    try:
        print("1. Testing basic connectivity...")
        with httpx.Client(timeout=10.0) as client:
            response = client.get(f"{API_URL}/health")
            print(f"   ✓ Health check: {response.status_code} - {response.json()}")
    except httpx.ConnectError as e:
        print(f"   ✗ Connection failed: {e}")
        print("\n   Possible causes:")
        print("   - No internet connection")
        print("   - Firewall blocking HTTPS traffic")
        print("   - Corporate proxy blocking the connection")
        print("   - DNS resolution issues")
        return False
    except Exception as e:
        print(f"   ✗ Unexpected error: {e}")
        return False
    
    try:
        print("\n2. Testing device code endpoint...")
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                f"{API_URL}/auth/github/device/code",
                headers={"Accept": "application/json"}
            )
            data = response.json()
            print(f"   ✓ Device code: {response.status_code}")
            print(f"   ✓ Got user_code: {data.get('user_code', 'N/A')}")
    except Exception as e:
        print(f"   ✗ Device code failed: {e}")
        return False
    
    print("\n" + "=" * 60)
    print("✓ All tests passed! The backend is reachable.")
    print("=" * 60)
    return True

if __name__ == "__main__":
    success = test_connection()
    sys.exit(0 if success else 1)
