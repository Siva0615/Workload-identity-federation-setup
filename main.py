import os
import sys
import time
import glob
import shutil
import subprocess
import json
import signal
import hashlib
import base64
import re
import pandas as pd
import zipfile
import numpy as np
from filelock import FileLock, Timeout
import logging
from logging.handlers import RotatingFileHandler
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from datetime import datetime, timezone

# === USER CONFIGURATION ===
GCP_PROJECT_ID = "nld-data-pltf-acquiring-prod"  # change dev/qa/prod as needed
GCLOUD_PATH = r"D:\Tools\google-cloud-sdk-517.0.0-windows-x86_64-bundled-python\google-cloud-sdk\bin\gcloud.cmd"
DIRECTORY_PATH = "//swnas-01.core.zone/pwcdataswan$/SrcFiles/INPUT/TDS"


# <<< MODIFIED: Renamed variables for clarity and consistency
PROCCESSED_FILES_DIRECTORY = f"{DIRECTORY_PATH}/PROCESSED"
IN_DIRECTORY = f"{DIRECTORY_PATH}/IN"
ARCHIVE_DIRECTORY = f"{DIRECTORY_PATH}/temp_processing"  # New temporary folder
STAGING_DIRECTORY = f"{DIRECTORY_PATH}/staging_files"

ie_archive_folder = f"{DIRECTORY_PATH}/ictf_files_archive"
error_folder = f"{DIRECTORY_PATH}/error_files"
service_account_key = r"D:\Tools\google-cloud-sdk-517.0.0-windows-x86_64-bundled-python\google-cloud-sdk\bin\config_files_cloudsetup_authent\nld-data-pltf-acquiring-prod-5abd33991c30.json"
kms_key_name = "projects/nld-data-pltf-acquiring-prod/locations/europe/keyRings/prod_swan_inbound_encryption-PhwwT/cryptoKeys/prod_swan_inbound_encryption_kms_key"

DEFAULT_GCS_BUCKET = f"gs://{GCP_PROJECT_ID}-swan-inbound/in/swan-inbound"
critical_error_log_name = "ie-uploader-activity"
activity_log_name = "ie-uploader-activity"

IE_ACQUIRER_IDS = {"00673072009", "00673005005", "00673002008"}

is_ctrlc_exit = False
gcloud_auth_success = False
processed_files_cache = set()
FILE_STABILITY_WAIT_SECONDS = 300 # Wait up to 5 minutes for a file to become stable
HEARTBEAT_INTERVAL_SECONDS = 300  # 5 minutes

# Logging setup
log_file_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ie-uploader.log"
)
logger = logging.getLogger("IEUploaderLogger")
logger.setLevel(logging.DEBUG)
file_handler = RotatingFileHandler(
    log_file_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(funcName)s] - %(message)s")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)


def log_and_print(
    message: str, level="info"
):  # This function is kept for compatibility with existing calls.
    getattr(logger, level.lower(), logger.info)(message)


def ensure_directory_exists(path):
    if not os.path.exists(path):
        log_and_print(f"Creating directory at {path}", "info")
        os.makedirs(path, exist_ok=True)


ensure_directory_exists(PROCCESSED_FILES_DIRECTORY)
ensure_directory_exists(IN_DIRECTORY)
ensure_directory_exists(error_folder)
ensure_directory_exists(ie_archive_folder)
ensure_directory_exists(ARCHIVE_DIRECTORY)  # Ensure the new temp folder exists
ensure_directory_exists(STAGING_DIRECTORY)


def authenticate_gcloud():
    global gcloud_auth_success
    logger.info("Authenticating with gcloud service account...")
    if not os.path.exists(service_account_key):
        raise Exception(f"Key file not found: {service_account_key}")
    # <<< FIX: Use 'cmd /c' to run the .cmd file and pass arguments as a list
    command = [
        'cmd', '/c',
        GCLOUD_PATH,
        "auth",
        "activate-service-account",
        f"--key-file={service_account_key}",
    ]
    result = subprocess.run(
        command,
        capture_output=True,
    )
    if result.returncode != 0:
        stderr_msg = result.stderr.decode().strip()
        logger.error(f"gcloud auth error: {stderr_msg}")
        raise Exception("Failed to authenticate with gcloud. Exiting.")
    logger.info("gcloud authentication successful.")
    gcloud_auth_success = True
    return True


def wait_file_ready(file_path, total_wait_seconds=FILE_STABILITY_WAIT_SECONDS, check_interval_seconds=2):
    """
    Waits for a file to be stable (size not changing and not locked) for a specified duration.
    """
    start_time = time.time()
    last_size = -1
    
    while time.time() - start_time < total_wait_seconds:
        try:
            if not os.path.exists(file_path):
                logger.warning(f"File '{os.path.basename(file_path)}' no longer exists. Skipping.")
                return False, "File disappeared"

            current_size = os.path.getsize(file_path)
            
            if current_size == last_size:
                # Size is stable, now check if it's locked
                try:
                    with open(file_path, 'rb'):
                        pass # Successfully opened, so it's not locked
                    
                    if current_size > 0:
                        logger.debug(f"File '{os.path.basename(file_path)}' is stable, unlocked, and not empty.")
                        return True, "File is stable"
                    else:
                        logger.warning(f"File '{os.path.basename(file_path)}' is stable but empty.")
                        return False, "File is empty"
                except (IOError, PermissionError):
                    logger.debug(f"File '{os.path.basename(file_path)}' size is stable but file is locked. Waiting...")
            
            last_size = current_size
            time.sleep(check_interval_seconds)

        except FileNotFoundError:
            logger.warning(f"File '{os.path.basename(file_path)}' was removed during stability check. Skipping.")
            return False, "File disappeared"

    logger.warning(f"File '{os.path.basename(file_path)}' did not stabilize within the {total_wait_seconds} second wait period.")
    return False, "File is unstable or locked"


def get_file_md5_base64(file_path):
    try:
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        md5_b64 = base64.b64encode(hash_md5.digest()).decode()
        logger.debug(f"MD5 (base64) for '{file_path}': {md5_b64}")
        return md5_b64
    except Exception as e:
        logger.error(
            f"Could not compute MD5 hash for '{file_path}'. Error: {str(e)}",
            exc_info=True,
        )
        return None


def move_to_error_folder(file_path, reason):
    file_name = os.path.basename(file_path)
    dest_path = os.path.join(error_folder, file_name)
    try:
        shutil.move(file_path, dest_path)
        logger.error(f"Moved '{file_name}' to error folder due to: {reason}")
        send_cloud_log_entry(
            severity="ERROR",
            message=f"[Pattern error] '{file_name}' did not match required pattern and was moved to error folder. Reason: {reason}",
            log_name=critical_error_log_name,
            data={
                "event_type": "FilePatternError",
                "file": file_name,
                "reason": reason,
            },
        )
    except Exception as e:
        logger.critical(
            f"Failed to move '{file_name}' to error folder: {str(e)}", exc_info=True
        )

def move_to_staging_folder(file_path, reason):
    """Moves a file to the staging folder for a later retry attempt."""
    file_name = os.path.basename(file_path)
    dest_path = os.path.join(STAGING_DIRECTORY, file_name)
    try:
        shutil.move(file_path, dest_path)
        logger.warning(f"Moved '{file_name}' to staging folder for retry. Reason: {reason}")
        send_cloud_log_entry(
            severity="WARNING",
            message=f"File '{file_name}' was moved to the staging folder for retry. Reason: {reason}",
            log_name=activity_log_name,
            data={
                "event_type": "FileMoveToStaging",
                "file": file_name,
                "reason": reason,
            },
        )
    except Exception as e:
        logger.critical(
            f"CRITICAL: Failed to move '{file_name}' to staging folder. It will be retried from the source folder. Error: {str(e)}",
            exc_info=True,
        )

def send_cloud_log_entry(
    severity="INFO", message="", log_name="ie-uploader-activity", data=None
):
    max_retries = 3
    base_delay_seconds = 5

    if data is None:
        data = {}
    payload = {
        "severity": severity,
        "message": message,
        "script_name": os.path.abspath(sys.argv[0]),
        "host": os.environ.get("COMPUTERNAME", ""),
        "watch_folder": PROCCESSED_FILES_DIRECTORY,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    payload.update(data)
    # The payload is a single string argument for the --json-payload flag.
    # json.dumps ensures it's a valid JSON string.
    json_payload_str = json.dumps(payload)

    for attempt in range(max_retries):
        try:
            # Pass the JSON payload as a separate argument to avoid shell quoting issues on Windows.
            command = [
                'cmd', '/c',
                GCLOUD_PATH,
                "logging",
                "write",
                log_name,
                json_payload_str,
                f"--project={GCP_PROJECT_ID}",
            ]
            result = subprocess.run(
                command,
                capture_output=True,
                check=False, # Explicitly set to False to handle errors manually
            )
            if result.returncode == 0:
                logger.debug(
                    f"Sent log to Cloud Logging (logName: {log_name}, severity: {severity})."
                )
                return # Success, exit the function
            else:
                # Raise an exception to be caught and retried
                raise Exception(
                    f"gcloud logging write command failed with exit code {result.returncode}: {result.stderr.decode().strip()}"
                )
        except Exception as e:
            logger.warning(
                f"Attempt {attempt + 1}/{max_retries} to send log failed. Error: {str(e)}"
            )
            if attempt < max_retries - 1:
                delay = base_delay_seconds * (2 ** attempt) # Exponential backoff
                logger.info(f"Retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                logger.critical(f"Failed to send log entry to Google Cloud after {max_retries} attempts.", exc_info=True)
                # The original exception 'e' is available here if you need to re-raise it or handle it further.


def encrypt_upload_and_archive(file_path):
    file_name = os.path.basename(file_path)
    lock_path = file_path + ".lock"
    lock = FileLock(lock_path, timeout=1)

    try:
        with lock:
            return _process_locked_encrypt_file(file_path)
    except Timeout:
        logger.debug(f"File '{file_name}' is locked by another process. Skipping.")
        return False

def _process_locked_encrypt_file(file_path):
    max_retries = 3
    retry_delay_seconds = 5
    file_name = os.path.basename(file_path)

    # Determine the GCS object name based on the output file naming convention
    if "_" in file_name:
        gcs_object_name = file_name.split("_", 1)[-1]
    else:
        gcs_object_name = file_name
    gcs_path = f"{DEFAULT_GCS_BUCKET}/{gcs_object_name}"
    log_name = activity_log_name
    alert_name = "FileEncryptedUploadSuccess"
    logger.info(f"Starting encryption and upload for '{file_name}'")

    # --- NEW: Retry loop for file stability check ---
    max_stability_retries = 5
    is_ready = False
    reason = "Unknown"
    for stability_attempt in range(max_stability_retries):
        logger.info(f"Checking file stability for '{file_name}' (Attempt {stability_attempt + 1}/{max_stability_retries})...")
        is_ready, reason = wait_file_ready(file_path)
        if is_ready:
            break # File is ready, exit the loop
        elif reason == "File disappeared" or reason == "File is empty":
            break # No point in retrying if file is gone or empty
        if stability_attempt < max_stability_retries - 1:
            logger.info(f"Will re-check stability for '{file_name}' in the next main loop cycle.")
    
    if not is_ready:
        logger.error(f"FAILURE: File '{file_name}' was not ready after {max_stability_retries} attempts. Reason: {reason}. Moving to error folder.")
        move_to_error_folder(file_path, f"File not stable after {max_stability_retries} attempts ({max_stability_retries * (FILE_STABILITY_WAIT_SECONDS/60):.0f} mins total). Reason: {reason}")
        return False

    for attempt in range(max_retries):
        # --- FIX: Create the temporary encrypted file in the ARCHIVE_DIRECTORY ---
        # This prevents the script from re-processing its own output.
        encrypted_output_file_name = file_name + ".enc"
        encrypted_output_file_path = os.path.join(ARCHIVE_DIRECTORY, encrypted_output_file_name)
        try:
            # 1. Generate a new Data Encryption Key (DEK) for each file.
            # --- Envelope Encryption (DEK) Implementation ---
            dek = AESGCM.generate_key(bit_length=256)
            logger.info(f"Generated a 256-bit DEK for '{file_name}'.")

            # 2. Encrypt (wrap) the DEK using the Cloud KMS key.
            logger.info(
                f"Attempt {attempt + 1}/{max_retries}: Wrapping the DEK for '{file_name}' using Cloud KMS..."
            )
            key_parts = kms_key_name.split('/')
            kms_project = key_parts[1]
            kms_location = key_parts[3]
            kms_keyring = key_parts[5]
            kms_key = key_parts[7]

            wrap_dek_command = [
                'cmd', '/c', GCLOUD_PATH, "kms", "encrypt",
                f"--project={kms_project}",
                f"--location={kms_location}",
                f"--keyring={kms_keyring}",
                f"--key={kms_key}",
                "--plaintext-file=-",      # Read plaintext from stdin
                "--ciphertext-file=-",     # Write ciphertext to stdout
            ]
            wrap_proc = subprocess.run(
                wrap_dek_command,
                input=dek,
                capture_output=True,
                check=True
            )
            wrapped_dek = wrap_proc.stdout
            logger.info(f"Successfully wrapped the DEK for '{file_name}'.")

            # 3. Encrypt the actual file data locally using the DEK.
            logger.info(f"Encrypting file content of '{file_name}' locally with the DEK.")
            with open(file_path, "rb") as f_in:
                plaintext_data = f_in.read()

            aes_gcm = AESGCM(dek)
            nonce = os.urandom(12)  # GCM recommended nonce size
            encrypted_data = aes_gcm.encrypt(nonce, plaintext_data, None)

            # 4. Write the combined encrypted file (wrapped_dek_len + wrapped_dek + nonce + encrypted_data).
            with open(encrypted_output_file_path, "wb") as f_out:
                f_out.write(len(wrapped_dek).to_bytes(4, 'big'))
                f_out.write(wrapped_dek)
                f_out.write(nonce)
                f_out.write(encrypted_data)
            logger.info(f"Created combined encrypted file: '{os.path.basename(encrypted_output_file_path)}'.")

            # 5. Upload the final encrypted file to GCS.
            logger.info(f"Uploading encrypted file '{os.path.basename(encrypted_output_file_path)}' to GCS path '{gcs_path}'...")
            upload_command = [
                'cmd', '/c', GCLOUD_PATH, "storage", "cp", encrypted_output_file_path, gcs_path,
                f"--project={GCP_PROJECT_ID}",
            ]
            upload_result = subprocess.run(upload_command, capture_output=True, check=True, timeout=900)

            if upload_result.returncode == 0:
                logger.info(f"SUCCESS: Upload of encrypted file '{gcs_object_name}' complete.")
                archive_path = os.path.join(ie_archive_folder, file_name)
                shutil.move(file_path, archive_path)
                logger.info(f"Archived original file '{file_name}' to '{archive_path}'.")

                msg = f"[{alert_name}] '{gcs_object_name}' was encrypted via Envelope Encryption and uploaded to bucket. Original file archived."
                send_cloud_log_entry(
                    severity="INFO",
                    message=msg,
                    log_name=log_name,
                    data={
                        "event_type": alert_name,
                        "file": file_name,
                        "bucket_path": gcs_path,
                        "archive_path": archive_path,
                        "encryption_method": "manual-kms-envelope",
                    },
                )
                logger.info("="*80 + "\n") # End of process separator
                # --- FIX: Clean up the temporary encrypted file on success ---
                if os.path.exists(encrypted_output_file_path):
                    os.remove(encrypted_output_file_path)
                    logger.debug(f"Cleaned up temporary encrypted file: '{encrypted_output_file_path}'.")
                return True  # Success, exit the function

        except subprocess.TimeoutExpired:
            error_msg = f"Attempt {attempt + 1}/{max_retries} FAILED for '{file_name}' due to a timeout. The upload took too long."
            logger.error(error_msg)
            move_to_staging_folder(file_path, "GCS upload timed out")
            return False # Exit retry loop, move to staging

        except Exception as e:
            logger.error(
                f"Attempt {attempt + 1}/{max_retries} FAILED for '{file_name}' with a gcloud/network exception: {str(e)}",
                exc_info=True,
            )
            # If the exception has stderr (from a subprocess failure), log it.
            if hasattr(e, 'stderr') and e.stderr:
                 logger.error(f"  Stderr: {e.stderr.decode()}")

        # If not the last attempt, wait before retrying
        # For gcloud errors, we move to staging instead of retrying immediately.
        logger.warning(f"Moving '{file_name}' to staging due to a gcloud/network error.")
        move_to_staging_folder(file_path, f"A gcloud subprocess failed: {str(e)}")
        return False

    # If all retries fail
    # This part is now less likely to be reached as we move to staging on first gcloud error.
    # Kept for logical completeness in case of other retryable local errors.
    logger.error(f"FAILURE: All attempts to process '{file_name}' failed. Moving to error folder.")
    move_to_error_folder(file_path, f"Upload failed after {max_retries} retries")
    logger.error("="*80 + "\n")
    # --- FIX: Clean up the temporary encrypted file on failure ---
    if os.path.exists(encrypted_output_file_path):
        os.remove(encrypted_output_file_path)
        logger.debug(f"Cleaned up temporary encrypted file: '{encrypted_output_file_path}'.")

    return False


def process_ie_file(file_path):
    """
    Processes a raw IE file, filters it based on acquirer IDs, and writes
    the result to the zip_watch_folder for subsequent encryption and upload.
    """
    file_name = os.path.basename(file_path)
    lock_path = file_path + ".lock"
    lock = FileLock(lock_path, timeout=1)

    try:
        with lock:
            return _process_locked_ie_file(file_path)
    except Timeout:
        logger.debug(f"File '{file_name}' is locked by another process. Skipping.")
        return False

def _process_locked_ie_file(file_path):
    file_name = os.path.basename(file_path)
    if file_name in processed_files_cache:
        return True

    logger.info("\n" + "="*80)
    logger.info(f"START: Filtering and zipping IE file '{file_name}'")

    # --- NEW: Retry loop for file stability check ---
    max_stability_retries = 5
    is_ready = False
    reason = "Unknown"
    for stability_attempt in range(max_stability_retries):
        logger.info(f"Checking IE file stability for '{file_name}' (Attempt {stability_attempt + 1}/{max_stability_retries})...")
        is_ready, reason = wait_file_ready(file_path)
        if is_ready:
            break # File is ready, exit the loop
        elif reason == "File disappeared" or reason == "File is empty":
            break # No point in retrying if file is gone or empty
        if stability_attempt < max_stability_retries - 1:
            logger.info(f"Will re-check stability for '{file_name}' in the next main loop cycle.")

    if not is_ready:
        logger.error(f"FAILURE: IE File '{file_name}' was not ready after {max_stability_retries} attempts. Reason: {reason}. Moving to error folder.")
        move_to_error_folder(file_path, f"IE file not stable after {max_stability_retries} attempts ({max_stability_retries * (FILE_STABILITY_WAIT_SECONDS/60):.0f} mins total). Reason: {reason}")
        logger.warning("="*80 + "\n")
        processed_files_cache.add(file_name)
        return False

    if "_" in file_name:
        output_file_name = file_name.split("_", 1)[-1]
    else:
        output_file_name = file_name

    output_path = os.path.join(IN_DIRECTORY, output_file_name)

    temp_file_name = file_name + ".tmp"
    temp_file_path = os.path.join(ARCHIVE_DIRECTORY, temp_file_name)
    try:
        shutil.copy(file_path, temp_file_path)
        logger.debug(f"Created temporary copy for processing: '{temp_file_path}'")

        with open(temp_file_path, "r", encoding="windows-1252") as f:
            lines = [line.rstrip("\n") for line in f]

        df = pd.DataFrame({"line": lines})

        df["batch_sequence_start"] = df["line"].str.startswith("01")
        df["batch_sequence"] = df["batch_sequence_start"].cumsum()

        transaction_prefixes = ("11", "40", "43", "65", "61", "71")
        transaction_start = df["line"].str.startswith(transaction_prefixes).astype(int)
        df["transaction_start"] = transaction_start
        df["transaction_sequence"] = df["transaction_start"].cumsum()
        
        # --- REFACTORED: Use np.select for more efficient acquirer_id extraction ---
        conditions = [
            df['line'].str.startswith('11'),
            df['line'].str.startswith('40'),
            df['line'].str.startswith('43'),
            df['line'].str.startswith('61'),
            df['line'].str.startswith('65'),
            df['line'].str.startswith('71')
        ]
        choices = [
            df['line'].str.slice(start=7, stop=18),
            df['line'].str.slice(start=15, stop=26),
            df['line'].str.slice(start=15, stop=26),
            df['line'].str.slice(start=6, stop=17),
            df['line'].str.slice(start=4, stop=15),
            df['line'].str.slice(start=120, stop=131)
        ]
        df['acquirer_id'] = np.select(conditions, choices, default=None)

        df["acquirer_id"] = df.groupby("transaction_sequence")["acquirer_id"].transform(
            "first"
        )
        df.loc[df["line"].str.startswith(("01", "98", "99", "00")), "acquirer_id"] = (
            np.nan
        )

        filtered_df = df[
            (df["acquirer_id"].isin(IE_ACQUIRER_IDS)) | (df["acquirer_id"].isnull())
        ]

        # Write the filtered content to the output file
        with open(output_path, "w", encoding="utf-8") as f:
            for line in filtered_df["line"]:
                f.write(line + "\n")
        logger.info(
            f"Filtered content from '{file_name}' into '{output_file_name}'."
        )

        # --- NEW: Zip the processed file ---
        zip_output_path = output_path + ".zip"
        logger.info(f"Compressing '{output_file_name}' into '{os.path.basename(zip_output_path)}'...")
        with zipfile.ZipFile(zip_output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(output_path, arcname=output_file_name)
        
        # Clean up the original unzipped file
        os.remove(output_path)
        logger.info(f"COMPLETED: Filtering and zipping for '{file_name}'. Output is '{os.path.basename(zip_output_path)}'.")

        logger.info(f"Original IE file '{file_name}' remains in '{PROCCESSED_FILES_DIRECTORY}'.")
        processed_files_cache.add(file_name)
        return True

    except Exception as e:
        logger.error(f"Failed to process IE file '{file_name}': {e}", exc_info=True)
        logger.error("="*80 + "\n")
        processed_files_cache.add(
            file_name
        )
        # Clean up partial files if they exist on error
        if os.path.exists(output_path):
            os.remove(output_path)
        return False
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)


def handle_ctrlc(sig, frame):
    global is_ctrlc_exit
    logger.critical(
        "\nCtrl+C detected. Initiating graceful shutdown. Will exit after the current file is processed."
    )
    is_ctrlc_exit = True
    send_cloud_log_entry(
        severity="CRITICAL",
        message="Shutdown signal received (Ctrl+C or window close). Script will exit gracefully.",
        log_name=critical_error_log_name,
        data={"exit_type": "CtrlC", "event_type": "CriticalShutdown"},
    )


signal.signal(signal.SIGINT, handle_ctrlc)


def main():
    global is_ctrlc_exit
    # Take a snapshot of files present at startup to ignore them.
    logger.info(
        f"Scanning for existing files in '{PROCCESSED_FILES_DIRECTORY}' to ignore..."
    )
    initial_ie_files = set(glob.glob(os.path.join(PROCCESSED_FILES_DIRECTORY, "*")))
    logger.info(f"Found {len(initial_ie_files)} existing files to ignore. Now watching for new files.")
    authenticate_gcloud()
    send_cloud_log_entry(
        severity="INFO",
        message="GCS IE Uploader script started and is now monitoring incoming files for upload.",
        log_name=activity_log_name,
        data={"event_type": "ScriptStartup"},
    )
    logger.info(
        f"Monitoring folders: '{PROCCESSED_FILES_DIRECTORY}' and '{IN_DIRECTORY}'. Press Ctrl+C to stop."
    )

    last_heartbeat_time = time.time()

    try:
        while True:
            if is_ctrlc_exit:
                break
            current_time = time.time()
            if current_time - last_heartbeat_time >= HEARTBEAT_INTERVAL_SECONDS:
                send_cloud_log_entry(
                    severity="INFO",
                    message="Heartbeat: IE Uploader script is running normally.",
                    log_name=activity_log_name,
                    data={"event_type": "Heartbeat"},
                )
                last_heartbeat_time = current_time
                logger.debug("Sent heartbeat log entry.")

            # --- PRIORITY 1: Process staged files ---
            staged_files = glob.glob(os.path.join(STAGING_DIRECTORY, "*.zip"))
            if staged_files:
                logger.info(f"Found {len(staged_files)} file(s) in staging. Entering retry mode.")
                if not authenticate_gcloud():
                    logger.warning("Authentication failed. Cannot process staged files. Will retry in 30 seconds...")
                    time.sleep(30)
                    continue

                for file_path in staged_files:
                    if is_ctrlc_exit: break
                    if os.path.isfile(file_path):
                        logger.info(f"Retrying staged file: {os.path.basename(file_path)}")
                        encrypt_upload_and_archive(file_path)
                continue # Loop again to re-check staging folder

            # --- Process NEW IE files ---
            # Check for files that were not present at startup.
            current_ie_files = set(glob.glob(os.path.join(PROCCESSED_FILES_DIRECTORY, "*")))
            new_ie_files = current_ie_files - initial_ie_files
            for file_path in new_ie_files:
                if is_ctrlc_exit: break
                if os.path.isfile(file_path):
                    file_name = os.path.basename(file_path)
                    # Derive the output name the same way process_ie_file does
                    # (strip leading prefix up to first underscore, if present)
                    output_base_name = file_name.split("_", 1)[-1] if "_" in file_name else file_name
                    # The archive stores the zipped form of the output file
                    archived_zip_name = output_base_name + ".zip"
                    archive_zip_path = os.path.join(ie_archive_folder, archived_zip_name)
                    # Also check for an exact-name match in case it was stored without extension
                    archive_exact_path = os.path.join(ie_archive_folder, file_name)
                    if os.path.exists(archive_zip_path) or os.path.exists(archive_exact_path):
                        matched_path = archive_zip_path if os.path.exists(archive_zip_path) else archive_exact_path
                        logger.warning(f"Duplicate detected: '{file_name}' already exists in archive folder as '{os.path.basename(matched_path)}'. Moving to error folder.")
                        send_cloud_log_entry(
                            severity="WARNING",
                            message=f"Duplicate file '{file_name}' detected in PROCESSED folder. File already exists in archive as '{os.path.basename(matched_path)}'. Moved to error folder.",
                            log_name=activity_log_name,
                            data={
                                "event_type": "DuplicateFileDetected",
                                "file": file_name,
                                "archive_path": matched_path,
                            },
                        )
                        move_to_error_folder(file_path, f"Duplicate file: '{file_name}' already exists in archive folder as '{os.path.basename(matched_path)}'")
                        continue
                    process_ie_file(file_path)
                    initial_ie_files.add(file_path)

            encrypt_files = glob.glob(os.path.join(IN_DIRECTORY, "*.zip"))
            for file_path in encrypt_files:
                if is_ctrlc_exit: break
                if os.path.isfile(file_path):
                    # This function now moves the original .zip file upon success or failure,
                    # so it won't be processed again in the next loop.
                    encrypt_upload_and_archive(file_path)

            time.sleep(1)


    except Exception as e:
        error_message = str(e)
        logger.critical(
            f"CRITICAL SCRIPT ERROR: Script terminated unexpectedly. Error: {error_message}",
            exc_info=True,
        )
        send_cloud_log_entry(
            severity="ERROR",
            message=f"GCS IE Uploader script terminated unexpectedly: {error_message}",
            log_name=critical_error_log_name,
            data={"error_type": "UnexpectedTermination", "full_error": error_message},
        )
        raise
    finally:
        logger.info(
            f"Script finished. Check Cloud Logging '{critical_error_log_name}' for details if an error occurred."
        )


def run_script_with_retries(max_retries=3, delay_seconds=10):
    attempt = 0
    while attempt < max_retries:
        try:
            main()
            break
        except Exception as e:
            attempt += 1
            logger.error(
                f"Attempt {attempt} of {max_retries} failed with exception: {str(e)}",
                exc_info=True,
            )
            if attempt < max_retries:
                logger.info(f"Retrying after {delay_seconds} seconds...")
                time.sleep(delay_seconds)
            else:
                logger.critical("Maximum retry attempts reached. Script will exit.")
                send_cloud_log_entry(
                    severity="CRITICAL",
                    message=f"IE Uploader script failed after {max_retries} retries. Manual intervention required.",
                    log_name=critical_error_log_name,
                    data={"event_type": "MaxRetryFailure"},
                )
                sys.exit(1)


if __name__ == "__main__":
    run_script_with_retries()
