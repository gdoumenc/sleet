import base64
import hashlib
import os
import shutil
import tempfile
import click

from pathlib import Path

from coworks.mixins import Boto3Mixin, AwsS3Session
from .command import CwsCommand


class CwsZipArchiver(CwsCommand, Boto3Mixin):
    def __init__(self, app=None, name='zip'):
        super().__init__(app, name=name)

    @property
    def options(self):
        return (
            click.option('--customer', '-c'),
            click.option('--bucket', '-b', help='Bucket to upload zip to'),
            click.option('--debug/--no-debug', default=False, help='Print debug logs to stderr.')
        )

    def _execute(self, options):
        aws_s3_session = AwsS3Session(profile_name='fpr-customer')

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            module_archive = shutil.make_archive(str(tmp_path.with_name('archive')), 'zip', options.project_dir)
            with open(module_archive, 'rb') as module_archive:
                b64sha256 = base64.b64encode(hashlib.sha256(module_archive.read()).digest())
                module_archive.seek(0)
                try:
                    archive_name = f"source_archives/{options.module}-{options.service}-{options['customer']}/archive.zip"
                    aws_s3_session.client.upload_fileobj(module_archive, options['bucket'], archive_name)
                    print(f"Successfully uploaded archive as {archive_name} ")
                except Exception as e:
                    print(f"Failed to upload module archive on S3 : {e}")

            with tmp_path.with_name('b64sha256_file').open('wb') as b64sha256_file:
                b64sha256_file.write(b64sha256)

            with tmp_path.with_name('b64sha256_file').open('rb') as b64sha256_file:
                try:
                    aws_s3_session.client.upload_fileobj(b64sha256_file, options['bucket'], f"{archive_name}.b64sha256",
                                                         ExtraArgs={'ContentType': 'text/plain'})
                    print(f"Successfully uploaded archive hash as {archive_name}.b64sha256, value of the hash : {b64sha256} ")
                except Exception as e:
                    print(f"Failed to upload archive hash on S3 : {e}")
