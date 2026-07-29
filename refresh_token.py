"""
Skripta koja automatski produzava (refresh) Instagram Access Token pre
nego sto istekne, i sama azurira GitHub Secret (IG_ACCESS_TOKEN) sa
novom vrednoscu -- bez ikakvog rucnog ucesca.

Ako produzavanje ne uspe (npr. token je vec nevazeci/ponisten od strane
Meta-e iz bezbednosnih razloga, ne samo prirodno istekao), skripta
automatski otvara GitHub Issue da odmah obavesti korisnika (GitHub
salje email notifikaciju o novom Issue-u, ako su podrazumevana
obavestenja ukljucena).

Pokrece se po rasporedu (GitHub Actions, jednom nedeljno) -- ne treba
rucno pokretanje.
"""

import os
import base64
import requests
from nacl import encoding, public

GITHUB_API = "https://api.github.com"
GRAPH_API = "https://graph.instagram.com"


def refresh_instagram_token(current_token):
    url = f"{GRAPH_API}/refresh_access_token"
    params = {
        "grant_type": "ig_refresh_token",
        "access_token": current_token,
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data["access_token"], data.get("expires_in")


def get_repo_public_key(repo, gh_token):
    url = f"{GITHUB_API}/repos/{repo}/actions/secrets/public-key"
    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def encrypt_secret(public_key_b64, secret_value):
    public_key = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def update_github_secret(repo, gh_token, secret_name, secret_value):
    key_data = get_repo_public_key(repo, gh_token)
    encrypted_value = encrypt_secret(key_data["key"], secret_value)

    url = f"{GITHUB_API}/repos/{repo}/actions/secrets/{secret_name}"
    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
    }
    payload = {
        "encrypted_value": encrypted_value,
        "key_id": key_data["key_id"],
    }
    r = requests.put(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()


def create_github_issue(repo, gh_token, title, body):
    url = f"{GITHUB_API}/repos/{repo}/issues"
    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
    }
    payload = {"title": title, "body": body}
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()


def main():
    current_token = os.environ["IG_ACCESS_TOKEN"]
    gh_token = os.environ["GH_PAT"]
    repo = os.environ["GITHUB_REPOSITORY"]  # automatski postavljeno od GitHub Actions

    try:
        new_token, expires_in = refresh_instagram_token(current_token)
        days = (expires_in // 86400) if expires_in else "?"
        print(f"Token uspesno produzen. Vazi jos ~{days} dana.")

        update_github_secret(repo, gh_token, "IG_ACCESS_TOKEN", new_token)
        print("GitHub Secret IG_ACCESS_TOKEN uspesno azuriran.")

    except Exception as e:
        print(f"Produzavanje tokena NIJE uspelo: {e}")
        try:
            create_github_issue(
                repo,
                gh_token,
                "Instagram token treba rucno obnavljanje",
                (
                    "Automatsko produzavanje Instagram access tokena nije uspelo:\n\n"
                    f"```\n{e}\n```\n\n"
                    "Ovo obicno znaci da je Meta iz bezbednosnih razloga ponistila "
                    "sesiju (ne samo prirodno isteklo trajanje). Potrebno je RUCNO "
                    "generisati nov token na developers.facebook.com i azurirati "
                    "IG_ACCESS_TOKEN secret."
                ),
            )
            print("GitHub Issue otvoren da te obavesti.")
        except Exception as issue_error:
            print(f"Ne mogu ni da otvorim GitHub Issue: {issue_error}")
        raise


if __name__ == "__main__":
    main()
