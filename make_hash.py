"""Django's command-line utility for generating hashes """
from django.contrib.auth.hashers import make_password
from django.conf import settings

def hash_password(password):
    """
    Generate a Django-compatible hash for a given password.

    :param password: the password to hash
    :return: the hashed password
    """
    return make_password(password)

if __name__ == "__main__":
    password = input("Enter a password: ")
    settings.configure()
    hashed_password = hash_password(password)
    print("Hashed password:", hashed_password)
