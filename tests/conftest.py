import os
from cryptography.fernet import Fernet
os.environ.setdefault('NEXORA_FERNET_KEY',Fernet.generate_key().decode())
