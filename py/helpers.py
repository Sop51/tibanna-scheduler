import os
import csv
import re
import sys
import boto3
from warnings import warn
from filetypes import get_filetype


def file_in_failed(subject):
    """
    Check if a subject is present in the "failed.txt" file.

    Args:
    subject (str): The subject to check.

    Returns:
    bool: True if the subject is present in "failed.txt", False otherwise.
    """
    with open("failed.txt", "r") as file:
        for line in file:
            if line.strip() == subject:
                return True
    return False

def get_subject_completed_set(outbucket, prefix="/mnt/data1/out/"):
    """
    check output bucket for completed subjects and return list them
    """
    s3_client = boto3.client('s3')
    response = s3_client.list_objects_v2(Bucket=outbucket, Prefix=prefix)
    completed_set = set()
    if 'Contents' in response:
        for obj in response['Contents']:
            if obj['Key'].endswith('.tar'):
                subj = extract_subjects(str(obj['Key']))
                for s in subj:
                    completed_set.add(s)

    return completed_set


def resolve_inputs(csv_file, batch_size, outbucket, cores_per_inst, allow_existing=False, exclude_failed=False):
    """
    takes a csv file with columns location, Subject to populate lists of each one
    constrained by other args

    args
    csv_file (str): must have columns "location, Subject"
    batch_size (int): max size to process at a time
    allow_existing (bool): excludes samples whose outputs are in the outbucket if false
    exclude_failed (bool): excludes files present in "failed.txt" if True
    """
    with open(csv_file, 'r') as file:
        reader = csv.DictReader(file)
        locations = []
        completed_set = get_subject_completed_set(outbucket) if not allow_existing else {}

        for row in reader:
            if row['Subject'] not in completed_set:
                location = row['location']
                if exclude_failed and file_in_failed(row['Subject']):
                    print(f"Skipping file for subject {row['Subject']} due to previous failure.")
                else:
                    locations.append(location)
            else:
                print(row['Subject'], " has already been called, skipping!")
            if len(locations) == batch_size:
                break

    # Adjusting locations to be a multiple of cores_per_inst
    locations = locations[:len(locations) - (len(locations) % cores_per_inst)]
    filenames = [loc.split("/")[-1] for loc in locations]

    return locations, filenames


def prepend_path(nested_list, path):
    """
    adds a specified path to a nested list of files

    args
    nested_list (list): nested list of any size
    path (str): path to prepend
    """
    if isinstance(nested_list, str):
        return path + nested_list
    else:
        return [prepend_path(item, path) for item in nested_list]
    

def basename(nested_list):
    """
    convert nested list of files to nest list of their basenames
    """
    if isinstance(nested_list, str):
        return os.path.splitext(os.path.basename(nested_list))[0]
    else:
        return [basename(item) for item in nested_list]


def group_inputs(filenames, items_per_list):
    """
    takes a list of filenames and desired nested list size
    and converts the flat list to nested

    args
    filenames (list[str]): flat list of filenames
    items_per_list (int): size of nested list
    """
    ftype, idx_ext = get_filetype(filenames)
    
    grouped_inputs = [filenames[i:i+items_per_list] for i in range(0, len(filenames), items_per_list)]
    grouped_input_paths = prepend_path(grouped_inputs, f"{ftype}s/")
    subjects = basename(grouped_inputs)
    if idx_ext:
        idx_filenames = [file + "." + idx_ext for file in filenames]
        grouped_idx = [idx_filenames[i:i+items_per_list] for i in range(0, len(filenames), items_per_list)] 
        grouped_idx_paths = prepend_path(grouped_idx, f"{ftype}sidx/")
    else:
        grouped_idx_paths = None

    return subjects, grouped_input_paths, grouped_idx_paths


def extract_subjects(string):
    """
    extract subject name from NIAGADS location string
    """
    pattern = re.compile(r'[A-Za-z-]+RS[\d-]+(?<!-)')
    matches = pattern.findall(string)

    return matches


def check_file_exists(bucket_name, file_key):
    """
    check if specfied file exists in a bucket
    """
    s3 = boto3.resource('s3')
    bucket = s3.Bucket(bucket_name)
    for obj in bucket.objects.filter(Prefix=file_key):
        if obj.key == file_key:
            return True
    
    return False


def move_logs_to_folder(jobid_prefix, outbucket):
    """
    move log files to folder matching their job_id prefix
    """
    s3 = boto3.client('s3')

    # Create the folder in the root of the S3 bucket
    s3.put_object(Bucket=outbucket, Key=f"{jobid_prefix}/")

    # List all objects in the bucket with the specified prefix
    response = s3.list_objects_v2(Bucket=outbucket, Prefix=f"{jobid_prefix}.")

    if 'Contents' in response:
        # Move each file with the specified prefix to the folder
        for file in response['Contents']:
            file_name = file['Key']
            new_key = f"{jobid_prefix}/{file_name}"
            s3.copy_object(Bucket=outbucket, Key=new_key, CopySource={'Bucket': outbucket, 'Key': file_name})
            s3.delete_object(Bucket=outbucket, Key=file_name)

    print(f"{jobid_prefix} logs consolidated successfully.")


def remove_inputs_from_file(filenames, inbucket):
    """
    remove cwl input files from inbucket based on given csv-file
    """
    ftype, idx_ext = get_filetype(filenames)
    s3 = boto3.client('s3')
    for file in filenames:
        try:
            s3.delete_object(Bucket=inbucket, Key=f"{ftype}s/{file}")
        except:
            warn(f"Could not find s3://{inbucket}/{ftype}s/{file}")
        if idx_ext:
            try:
                s3.delete_object(Bucket=inbucket, Key=f"{ftype}sidx/{file}.{idx_ext}")
            except:
                warn(f"Could not find s3://{inbucket}/{ftype}sidx/{file}.{idx_ext}")
    print("File deletion complete.")


def move_logs_to_root(jobid_prefix, outbucket):
    """
    move log files back to root 
    (neccessary for tibanna to find them!)
    """
    s3 = boto3.client('s3')

    # List all objects in the specified folder
    response = s3.list_objects_v2(Bucket=outbucket, Prefix=f"{jobid_prefix}/{jobid_prefix}.")

    if 'Contents' in response:
        # Move each file to the root directory
        for file in response['Contents']:
            file_name = file['Key']
            new_key = os.path.basename(file_name)
            s3.copy_object(Bucket=outbucket, Key=new_key, CopySource={'Bucket': outbucket, 'Key': file_name})
            s3.delete_object(Bucket=outbucket, Key=file_name)

    print(f"{jobid_prefix} logs moved to root directory successfully.")


def remove_all_inputs(inbucket, dirs=["cramsidx", "crams"]):
    """
    remove ALL cwl input files from inbucket 
    """
    s3 = boto3.client('s3')
    for dir in dirs:
        response = s3.list_objects_v2(Bucket=inbucket, Prefix=dir)
        if 'Contents' in response:
            # Delete each file in the folder
            objects = [{'Key': obj['Key']} for obj in response['Contents']]
            s3.delete_objects(Bucket=inbucket, Delete={'Objects': objects})

    print("Successfully removed all input files!")


def get_unique_job_ids_from_s3_bucket(bucket_name, jobid_prefix):
    """
    return unique job ids with the jobid_prefix
    """
    s3 = boto3.client('s3')

    # List objects in the S3 bucket
    response = s3.list_objects_v2(Bucket=bucket_name, Prefix=jobid_prefix)

    # Extract unique job IDs based on the specified pattern
    job_ids = set()
    for obj in response['Contents']:
        key = obj['Key']
        if key.endswith('.postrun.json'):
            job_id = key.split('.postrun.json')[0]
            job_ids.add(job_id)

    return list(job_ids)


def process_postrun_files(jobid_prefix, outbucket):
    """
    check post run for the md5sum substring to deteermine which runs did not complete
    """
    # Move jib id associated logs to root (necessary for tibanna to find)
    move_logs_to_root(jobid_prefix, outbucket)

    s3 = boto3.client('s3')
    
    # List objects in the S3 bucket with the specified prefix
    response = s3.list_objects_v2(Bucket=outbucket, Prefix=f"{jobid_prefix}.")

    failed_job_ids = set()  # Set to store failed job IDs

    for obj in response.get('Contents', []):
        key = obj['Key']
        if key.endswith('.postrun.json'):
            # Read the content of the .postrun.json file
            response = s3.get_object(Bucket=outbucket, Key=key)
            content = response['Body'].read().decode('utf-8')

            # Check if the content contains the "md5sum" string
            if '"md5sum":"' not in content:
                # Extract the job ID from the key
                job_id = key.split('.postrun.json')[0]
                failed_job_ids.add(job_id)

    print(f"Failed to complete {len(failed_job_ids)} jobs in the {jobid_prefix} batch!")
    # Append failed job IDs to the output file
    with open('failed_runs.txt', 'a') as f:
        for job_id in failed_job_ids:
            f.write(job_id + '\n')

    # Remove duplicates from the file
    lines = set()
    with open("failed_runs.txt", "r") as file:
        lines = file.readlines()
    with open("failed_runs.txt", "w") as file:
        file.writelines(set(lines))

    # move jid associated logs back to folder
    move_logs_to_folder(jobid_prefix, outbucket)

