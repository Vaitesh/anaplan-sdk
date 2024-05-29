"""
Asynchronous Client.
"""

import base64
import gzip
import logging
import os
import time
from asyncio import gather

import httpx
from cryptography.exceptions import InvalidKey, UnsupportedAlgorithm
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from httpx import HTTPStatusError, HTTPError
from tenacity import retry, retry_if_exception_type, stop_after_attempt

from ._exceptions import (
    InvalidCredentialsException,
    InvalidPrivateKeyException,
    InvalidIdentifierException,
    AnaplanActionError,
    ReAuthException,
)
from ._models import (
    Import,
    Export,
    Process,
    File,
    Action,
    List,
    Workspace,
    Model,
    to_workspaces,
    to_models,
    to_actions,
    to_imports,
    to_exports,
    to_processes,
    to_files,
    to_lists,
    determine_action_type,
)

logger = logging.getLogger("anaplan_sdk")


class AsyncClient:
    """
    An asynchronous Client for pythonic access to the Anaplan Integration API v2:
    https://anaplan.docs.apiary.io/. This Client provides high-level abstractions over the API, so
    you can deal with python objects and simple functions rather than implementation details like
    http, json, compression, chunking etc.


    For more information, quick start guides and detailed instructions refer to:
    https://vinzenzklass.github.io/anaplan-sdk.
    """

    def __init__(
        self,
        workspace_id: str | None = None,
        model_id: str | None = None,
        user_email: str | None = None,
        password: str | None = None,
        certificate: str | bytes | None = None,
        private_key: str | bytes | None = None,
        private_key_password: str | bytes | None = None,
        timeout: int = 30,
        status_poll_delay: int = 1,
        upload_chunk_size: int = 25_000_000,
    ) -> None:
        """
        An asynchronous Client for pythonic access to the Anaplan Integration API v2:
        https://anaplan.docs.apiary.io/. This Client provides high-level abstractions over the API,
        so you can deal with python objects and simple functions rather than implementation details
        like http, json, compression, chunking etc.


        For more information, quick start guides and detailed instructions refer to:
        https://vinzenzklass.github.io/anaplan-sdk.

        :param workspace_id: The Anaplan workspace Id. You can copy this from the browser URL or
                             find them using an HTTP Client like Postman, Paw, Insomnia etc.
        :param model_id: The identifier of the model.
        :param user_email: A valid email registered with the Anaplan Workspace you are attempting
                           to access. **The associated user must have Workspace Admin privileges**
        :param password: Password for the given `user_email`. This is not suitable for production
                         setups. If you intend to use this in production, acquire a client
                         certificate as described under: https://help.anaplan.com/procure-ca-certificates-47842267-2cb3-4e38-90bf-13b1632bcd44
        :param certificate: The absolute path to the client certificate file or the certificate
                            itself.
        :param private_key: The absolute path to the private key file or the private key itself.
        :param private_key_password: The password to access the private key if there is one.
        :param timeout: The timeout for the HTTP requests.
        :param status_poll_delay: The delay between polling the status of a task.
        :param upload_chunk_size: The size of the chunks to upload. This is the maximum size of
                                  each chunk. Defaults to 25MB.
        """
        if not ((user_email and password) or (certificate and private_key)):
            raise ValueError(
                "Either `certificate` and `private_key` or `user_email` and `password` must be "
                "provided."
            )
        self._client = httpx.AsyncClient()
        self._auth_url = "https://auth.anaplan.com/token/authenticate"
        self._base_url = "https://api.anaplan.com/2/0/workspaces"
        self.workspace_id = workspace_id
        self.model_id = model_id
        self.user_email = user_email
        self.password = password
        self.certificate = certificate
        self.private_key = private_key
        self.private_key_password = private_key_password
        self.timeout = timeout
        self.status_poll_delay = status_poll_delay
        self.upload_chunk_size = upload_chunk_size
        self._auth_token = ""
        self._cert_auth() if certificate else self._basic_auth()

    async def list_workspaces(self) -> list[Workspace]:
        """
        Lists all the Workspaces the authenticated user has access to.
        :return: All Workspaces as a list of :py:class:`Workspace`.
        """
        return to_workspaces(await self._get(f"{self._base_url}?tenantDetails=true"))

    async def list_models(self) -> list[Model]:
        """
        Lists all the Models the authenticated user has access to.
        :return: All Models in the Workspace as a list of :py:class:`Model`.
        """
        return to_models(
            await self._get(f"{self._base_url.replace('/workspaces', '/models')}?modelDetails=true")
        )

    async def list_actions(self) -> list[Action]:
        """
        Lists all the Actions in the Model. This will only return the Actions listed under
        `Other Actions` in Anaplan. For Imports, exports, and processes, see their respective
        methods instead.

        :return: All Actions on this model as a list of :py:class:`Action`.
        """
        return to_actions(
            await self._get(f"{self._base_url}/{self.workspace_id}/models/{self.model_id}/actions")
        )

    async def list_imports(self) -> list[Import]:
        """
        Lists all the Imports in the Model.
        :return: All Imports on this model as a list of :py:class:`Import`.
        """
        return to_imports(
            await self._get(f"{self._base_url}/{self.workspace_id}/models/{self.model_id}/imports")
        )

    async def list_exports(self) -> list[Export]:
        """
        Lists all the Exports in the Model.
        :return: All Exports on this model as a list of :py:class:`Export`.
        """
        return to_exports(
            await self._get(f"{self._base_url}/{self.workspace_id}/models/{self.model_id}/exports")
        )

    async def list_processes(self) -> list[Process]:
        """
        Lists all the Processes in the Model.
        :return: All Processes on this model as a list of :py:class:`Process`.
        """
        return to_processes(
            await self._get(
                f"{self._base_url}/{self.workspace_id}/models/{self.model_id}/processes"
            )
        )

    async def list_files(self) -> list[File]:
        """
        Lists all the Files in the Model.
        :return: All Files on this model as a list of :py:class:`File`.
        """
        return to_files(
            await self._get(f"{self._base_url}/{self.workspace_id}/models/{self.model_id}/files")
        )

    async def list_lists(self) -> list[List]:
        """
        Lists all the Lists in the Model.
        :return: All Lists on this model as a list of :py:class:`List`.
        """
        return to_lists(
            await self._get(f"{self._base_url}/{self.workspace_id}/models/{self.model_id}/lists")
        )

    async def run_action(self, action_id: int) -> None:
        """
        Runs the specified Anaplan Action and validates the spawned task. If the Action fails or
        completes with errors, will raise an :py:class:`AnaplanActionError`. Failed Tasks are
        usually not something you can recover from at runtime and often require manual changes in
        Anaplan, i.e. updating the mapping of an Import or similar. So, for convenience, this will
        raise an Exception to handle - if you for e.g. think that one of the uploaded chunks may
        have been dropped and simply retrying with new data may help - and not return the task
        status information that needs to be handled by the caller.

        If you need more information or control, you can use `invoke_action()` and
        `get_task_status()`.
        :param action_id: The identifier of the Action to run. Can be any Anaplan Invokable;
                          Processes, Imports, Exports, Other Actions.
        """
        task_id = await self.invoke_action(action_id)
        task_status = await self.get_task_status(action_id, task_id)

        while "COMPLETE" not in task_status.get("taskState"):
            time.sleep(self.status_poll_delay)
            task_status = await self.get_task_status(action_id, task_id)

        if task_status.get("taskState") == "COMPLETE" and not task_status.get("result").get(
            "successful"
        ):
            raise AnaplanActionError(f"Task '{task_id}' completed with errors.")

    async def get_file(self, file_id: int) -> bytes:
        """
        Retrieves the content of the specified file.
        :param file_id: The identifier of the file to retrieve.
        :return: The content of the file.
        """
        return await self._get_binary(
            f"{self._base_url}/{self.workspace_id}/models/{self.model_id}/files/{file_id}"
        )

    async def upload_file(self, file_id: int, content: str | bytes) -> None:
        """
        Uploads the content to the specified file. If `upload_parallel` is set to True on the
        instance you are invoking this from, will attempt to upload the chunks in parallel for
        better performance. If you are network bound or are experiencing rate limiting issues, set
        `upload_parallel` to False.

        :param file_id: The identifier of the file to upload to.
        :param content: The content to upload. **This Content will be compressed before uploading.
                        If you are passing the Input as bytes, pass it uncompressed to avoid
                        redundant work.**
        """
        if isinstance(content, str):
            content = content.encode()
        chunks = [
            content[i : i + self.upload_chunk_size]
            for i in range(0, len(content), self.upload_chunk_size)
        ]
        await self._set_chunk_count(file_id, len(chunks))
        await gather(
            *[self._upload_chunk(file_id, index, chunk) for index, chunk in enumerate(chunks)]
        )
        logger.info(f"Content loaded to  File '{file_id}'.")

    async def get_task_status(
        self, action_id: int, task_id: str
    ) -> dict[str, float | int | str | list | dict | bool]:
        """
        Retrieves the status of the specified task.
        :param action_id: The identifier of the action that was invoked.
        :param task_id: The identifier of the spawned task.
        :return: The status of the task as returned by the API. For more information
                 see: https://anaplan.docs.apiary.io.
        """
        return (
            await self._get(
                f"{self._base_url}/{self.workspace_id}/models/{self.model_id}/"
                f"{determine_action_type(action_id)}/{action_id}/tasks/{task_id}"
            )
        ).get("task")

    async def invoke_action(self, action_id: int) -> str:
        """
        You may want to consider using `run_action()` instead.

        Invokes the specified Anaplan Action and returns the spawned Task identifier. This is
        useful if you want to handle the Task status yourself or if you want to run multiple
        Actions in parallel.
        :param action_id:
        :return:
        """
        response = await self._post(
            f"{self._base_url}/{self.workspace_id}/models/{self.model_id}/"
            f"{determine_action_type(action_id)}/{action_id}/tasks",
            json={"localeName": "en_US"},
        )
        task_id = response.get("task").get("taskId")
        logger.info(f"Invoked Action '{action_id}', spawned Task: '{task_id}'.")
        return task_id

    @retry(retry=retry_if_exception_type(ReAuthException), stop=stop_after_attempt(2))
    async def _upload_chunk(self, file_id: int, index: int, chunk: bytes) -> None:
        try:
            response = await self._client.put(
                f"{self._base_url}/{self.workspace_id}/models/{self.model_id}/files/{file_id}/"
                f"chunks/{index}",
                headers={
                    "Authorization": f"AnaplanAuthToken {self._auth_token}",
                    "Content-Type": "application/x-gzip",
                },
                content=gzip.compress(chunk),
                timeout=self.timeout,
            )
            response.raise_for_status()
        except HTTPError as error:
            await self._recover_or_raise(error)

    async def _set_chunk_count(self, file_id: int, num_chunks: int) -> None:
        await self._post(
            f"{self._base_url}/{self.workspace_id}/models/{self.model_id}/files/{file_id}",
            json={"chunkCount": num_chunks},
        )

    async def _basic_auth(self) -> None:
        try:
            credentials = base64.b64encode(f"{self.user_email}:{self.password}".encode()).decode()
            response = await self._client.post(
                self._auth_url,
                headers={"Authorization": f"Basic {credentials}"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            self._auth_token = response.json().get("tokenInfo").get("tokenValue")
            logger.info("Authentication Token created.")
        except HTTPError as error:
            if isinstance(error, HTTPStatusError) and error.response.status_code == 401:
                raise InvalidCredentialsException from error
            raise error

    async def _cert_auth(self) -> None:
        try:
            message = os.urandom(150)
            encoded_cert = base64.b64encode(await self._get_certificate()).decode()
            encoded_string = base64.b64encode(message).decode()
            encoded_signed_string = base64.b64encode(
                (await self._get_private_key()).sign(message, padding.PKCS1v15(), hashes.SHA512())
            ).decode()
            payload = {"encodedData": encoded_string, "encodedSignedData": encoded_signed_string}
            response = await self._client.post(
                self._auth_url,
                headers={
                    "Authorization": f"CACertificate {encoded_cert}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            self._auth_token = response.json().get("tokenInfo").get("tokenValue")
            logger.info("Authentication Token created.")
        except HTTPError as error:
            if isinstance(error, HTTPStatusError) and error.response.status_code == 401:
                raise InvalidCredentialsException from error
            raise error

    @retry(retry=retry_if_exception_type(ReAuthException), stop=stop_after_attempt(2))
    async def _get(self, url: str) -> dict[str, float | int | str | list | dict | bool]:
        try:
            response = await self._client.get(
                url,
                headers={"Authorization": f"AnaplanAuthToken {self._auth_token}"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except HTTPError as error:
            await self._recover_or_raise(error)

    @retry(retry=retry_if_exception_type(ReAuthException), stop=stop_after_attempt(2))
    async def _get_binary(self, url: str) -> bytes:
        try:
            response = await self._client.get(
                url,
                headers={"Authorization": f"AnaplanAuthToken {self._auth_token}"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.content
        except HTTPError as error:
            await self._recover_or_raise(error)

    @retry(retry=retry_if_exception_type(ReAuthException), stop=stop_after_attempt(2))
    async def _post(
        self, url: str, json: dict | None = None
    ) -> dict[str, float | int | str | list | dict | bool]:
        try:
            return (
                await self._client.post(
                    url,
                    headers={
                        "Authorization": f"AnaplanAuthToken {self._auth_token}",
                        "Content-Type": "application/json",
                    },
                    json=json,
                    timeout=self.timeout,
                )
            ).json()
        except HTTPError as error:
            await self._recover_or_raise(error)

    async def _recover_or_raise(self, error: HTTPError) -> None:
        if isinstance(error, HTTPStatusError):
            if error.response.status_code == 401:
                await self._cert_auth() if self.certificate else await self._basic_auth()
                raise ReAuthException from error
            if error.response.status_code == 404:
                raise InvalidIdentifierException from error
        raise error

    async def _get_certificate(self) -> bytes:
        if isinstance(self.certificate, str):
            if os.path.isfile(self.certificate):
                async with open(self.certificate, "rb") as f:
                    return await f.read()
            return self.certificate.encode()
        return self.certificate

    async def _get_private_key(self) -> RSAPrivateKey:
        try:
            if isinstance(self.private_key, str):
                if os.path.isfile(self.certificate):
                    async with open(self.private_key, "rb") as f:
                        data = await f.read()
                else:
                    data = self.private_key.encode()
            else:
                data = self.private_key

            password = None
            if self.private_key_password:
                if isinstance(self.private_key_password, str):
                    password = self.private_key_password.encode()
                else:
                    password = self.private_key_password
            return serialization.load_pem_private_key(data, password, backend=default_backend())
        except (IOError, InvalidKey, UnsupportedAlgorithm) as error:
            raise InvalidPrivateKeyException from error
