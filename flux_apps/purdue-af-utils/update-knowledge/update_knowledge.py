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


def add_file_to_knowledge(file_id, force=False):
    url = f"{API_URL}v1/knowledge/{KNOWLEDGE_ID}/file/add"
    headers = {**HEADERS, "Content-Type": "application/json"}
    data = {"file_id": file_id}
    if force:
        data["force"] = True
    resp = requests.post(url, headers=headers, json=data)
    print(f"Add-to-knowledge status: {resp.status_code}")
    if resp.status_code == 400 and "Duplicate content detected" in resp.text:
        print(f"Warning: Duplicate content detected for file {file_id}")
        print(f"Full response: {resp.text}")
        if not force:
            print("Trying with force=True...")
            return add_file_to_knowledge(file_id, force=True)
        return "DUPLICATE"
    if resp.status_code != 200:
        print(f"Error response: {resp.text}")
        resp.raise_for_status()
    return resp.json()


def add_file_to_knowledge_alternative(file_id):
    """Alternative method to add file to knowledge base"""
    # Try different API endpoints or methods
    endpoints = [
        f"{API_URL}v1/knowledge/{KNOWLEDGE_ID}/files",
        f"{API_URL}v1/knowledge/{KNOWLEDGE_ID}/add",
        f"{API_URL}v1/knowledge/{KNOWLEDGE_ID}/upload",
    ]

    for endpoint in endpoints:
        try:
            print(f"Trying endpoint: {endpoint}")
            headers = {**HEADERS, "Content-Type": "application/json"}
            data = {"file_id": file_id}
            resp = requests.post(endpoint, headers=headers, json=data)
            print(f"Status: {resp.status_code}")
            if resp.status_code == 200:
                print(f"Success with endpoint: {endpoint}")
                return resp.json()
        except Exception as e:
            print(f"Error with endpoint {endpoint}: {e}")
            continue

    return "FAILED"


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


def clear_knowledge_base():
    """Clear all files from the knowledge base"""
    try:
        current_file_ids = list_knowledge_files()
        print(f"Clearing {len(current_file_ids)} files from knowledge base...")
        for file_id in current_file_ids:
            try:
                print(f"Removing file {file_id} from knowledge base...")
                remove_file_from_knowledge(file_id)
            except Exception as e:
                print(f"Error removing file {file_id} from knowledge: {e}")
        print("Knowledge base cleared.")
    except Exception as e:
        print(f"Error clearing knowledge base: {e}")


def clear_vector_database():
    """Clear the vector database to work around OpenWebUI bug #7181"""
    try:
        # Based on the GitHub issue solution, we need to properly clean up vector database entries
        # The solution involves deleting files and their vector database entries

        print("Clearing vector database using file-by-file cleanup...")

        # Get all files in the knowledge base
        current_files = list_knowledge_files()
        print(f"Found {len(current_files)} files to clean up")

        # Remove each file from knowledge base (this should trigger vector database cleanup)
        for file_id in current_files:
            try:
                print(f"Removing file {file_id} from knowledge base...")
                remove_file_from_knowledge(file_id)
                print(f"File {file_id} removed from knowledge base")
            except Exception as e:
                print(f"Error removing file {file_id} from knowledge: {e}")

        # Also delete all files from server storage to ensure complete cleanup
        server_files = list_all_server_files()
        print(f"Found {len(server_files)} files in server storage to delete")

        for file_info in server_files:
            file_id = file_info.get("id")
            try:
                print(f"Deleting file {file_id} from server storage...")
                delete_file(file_id)
                print(f"File {file_id} deleted from server storage")
            except Exception as e:
                print(f"Error deleting file {file_id} from server: {e}")

        print("Vector database cleanup completed")
        return True

    except Exception as e:
        print(f"Error clearing vector database: {e}")
        return False


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

        # Step 1: Clear everything including vector database (work around OpenWebUI bug #7181)
    print("Step 1: Clearing everything...")
    try:
        # Clear vector database and all files (this is the key fix for the bug)
        print("Clearing vector database to work around OpenWebUI bug #7181...")
        clear_vector_database()

        print("All files and vector database cleared successfully")
    except Exception as e:
        print(f"Error clearing files: {e}")
        return

    # Step 2: Upload and add files one by one
    rst_files = find_rst_files()
    print(f"Step 2: Found {len(rst_files)} .rst files to process.")

    added_count = 0
    for file_path in rst_files:
        try:
            print(f"Processing file: {file_path}")

            # Upload file
            file_id = upload_file(file_path)
            print(f"File uploaded with id: {file_id}")

            # Add to knowledge immediately
            result = add_file_to_knowledge(file_id)
            if result == "DUPLICATE":
                print(f"Duplicate detected, but checking if file was actually added...")
                # Check if the file is actually in the knowledge base despite the duplicate warning
                try:
                    current_files = list_knowledge_files()
                    if file_id in current_files:
                        print(
                            f"File {file_path} was actually added despite duplicate warning"
                        )
                        added_count += 1
                    else:
                        print(f"File {file_path} was not added to knowledge base")
                except Exception as e:
                    print(f"Error checking if file was added: {e}")
            else:
                print(f"File {file_path} added to knowledge")
                added_count += 1

        except Exception as e:
            print(f"Error processing {file_path}: {e}")
            continue

    print(f"Successfully processed {added_count} files")

    # Final verification
    print("Final verification: Checking knowledge base contents...")
    try:
        final_files = list_knowledge_files()
        print(f"Files in knowledge base: {len(final_files)}")
        if final_files:
            print("Files successfully added to knowledge base:")
            for file_id in final_files:
                print(f"  - {file_id}")
        else:
            print("WARNING: No files in knowledge base!")
    except Exception as e:
        print(f"Error checking final state: {e}")

    print(f"Knowledge base update complete!")


if __name__ == "__main__":
    try:
        import dotenv
    except ImportError:
        print("Please install python-dotenv: pip install python-dotenv")
        exit(1)
    main()
