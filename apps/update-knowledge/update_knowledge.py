import glob
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

# Load .env from the script's directory
SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(dotenv_path=SCRIPT_DIR / ".env")

API_URL = os.environ.get("OPENWEBUI_API_URL", "http://openwebui:3000/api")
KNOWLEDGE_ID = os.environ["KNOWLEDGE_ID"]
API_TOKEN = os.environ["OPENWEBUI_API_TOKEN"]
DELETE_ALL_FILES = os.environ.get("DELETE_ALL_FILES")
LIST_ALL_FILES = os.environ.get("LIST_ALL_FILES")

HEADERS = {"Authorization": f"Bearer {API_TOKEN}", "Accept": "application/json"}


def upload_file(file_path):
    url = f"{API_URL}v1/files/"
    with open(file_path, "rb") as f:
        files = {"file": f}
        resp = requests.post(url, headers=HEADERS, files=files)
    print(f"Upload status: {resp.status_code}")
    resp.raise_for_status()
    return resp.json()["id"]


def add_file_to_knowledge(file_id):
    url = f"{API_URL}v1/knowledge/{KNOWLEDGE_ID}/file/add"
    headers = {**HEADERS, "Content-Type": "application/json"}
    data = {"file_id": file_id}
    resp = requests.post(url, headers=headers, json=data)
    print(f"Add-to-knowledge status: {resp.status_code}")
    if resp.status_code == 400 and "Duplicate content detected" in resp.text:
        print("Warning: Duplicate content detected, skipping file")
        return "DUPLICATE"
    if resp.status_code != 200:
        print(f"Error response: {resp.text}")
        resp.raise_for_status()
    return resp.json()


def remove_file_from_knowledge(file_id):
    url = f"{API_URL}v1/knowledge/{KNOWLEDGE_ID}/file/remove"
    headers = {**HEADERS, "Content-Type": "application/json"}
    data = {"file_id": file_id}
    resp = requests.post(url, headers=headers, json=data)
    print(f"Remove-from-knowledge status: {resp.status_code}")
    resp.raise_for_status()
    return resp.json()


def delete_file(file_id):
    url = f"{API_URL}v1/files/{file_id}"
    resp = requests.delete(url, headers=HEADERS)
    print(f"Delete file status: {resp.status_code}")
    resp.raise_for_status()
    return resp.json()


def list_knowledge_files():
    url = f"{API_URL}v1/knowledge/{KNOWLEDGE_ID}"
    resp = requests.get(url, headers=HEADERS)
    print(f"List knowledge files status: {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()
    return [f["id"] for f in data.get("files", [])]


def list_all_server_files():
    url = f"{API_URL}v1/files/"
    resp = requests.get(url, headers=HEADERS)
    print(f"List all server files status: {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()
    # Handle both list and dict responses
    if isinstance(data, list):
        files = data
    else:
        files = data.get("files", [])
    print(f"Found {len(files)} files on server:")
    for file_info in files:
        print(
            f"  ID: {file_info.get('id')}, Name: {file_info.get('name')}, Size: {file_info.get('size')}"
        )
    return files


def delete_all_files():
    files = list_all_server_files()
    file_ids = [f.get("id") for f in files]
    print(f"Deleting {len(file_ids)} files from server...")
    for file_id in file_ids:
        print(f"Deleting file {file_id} from server...")
        delete_file(file_id)
    print("All files deleted.")


def find_rst_files():
    # Since we're now running from the cloned repo directory
    docs_source = Path("docs") / "source"
    print(f"Looking for .rst files in: {docs_source.absolute()}")
    print(f"Current working directory: {Path.cwd()}")

    if not docs_source.exists():
        print(f"ERROR: Directory {docs_source.absolute()} does not exist!")
        print(f"Available directories in current path:")
        for item in Path.cwd().iterdir():
            print(f"  - {item}")
        return []

    rst_files = []
    for file_path in docs_source.rglob("*.rst"):
        # Exclude files from _static, demos, and images directories
        if not any(part in ["_static", "demos", "images"] for part in file_path.parts):
            rst_files.append(file_path)
            print(f"Found .rst file: {file_path}")

    print(f"Total .rst files found: {len(rst_files)}")
    return rst_files


def main():
    if LIST_ALL_FILES:
        list_all_server_files()
        return

    # Delete all existing files at the beginning
    print("Cleaning up all existing files...")
    try:
        delete_all_files()
        print("All existing files deleted successfully")
    except Exception as e:
        print(f"Error deleting existing files: {e}")
        return

    # Upload new files
    rst_files = find_rst_files()
    print(f"Found {len(rst_files)} .rst files to upload.")

    uploaded_file_ids = []
    for file_path in rst_files:
        try:
            print(f"Uploading file: {file_path}")
            file_id = upload_file(file_path)
            print(f"File uploaded with id: {file_id}")
            result = add_file_to_knowledge(file_id)
            if result == "DUPLICATE":
                print(f"Warning: Unexpected duplicate detected for {file_path}")
            else:
                print(f"File added to knowledge")
                uploaded_file_ids.append(file_id)
        except Exception as e:
            print(f"Error processing {file_path}: {e}")
            continue

    print(f"Successfully uploaded {len(uploaded_file_ids)} files to knowledge base")


if __name__ == "__main__":
    try:
        import dotenv
    except ImportError:
        print("Please install python-dotenv: pip install python-dotenv")
        exit(1)
    main()
