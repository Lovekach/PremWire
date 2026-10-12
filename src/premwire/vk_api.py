from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request
from .http_transport import urlopen
from uuid import uuid4


class VkApiError(RuntimeError):
    """A deterministic VK API rejection safe to retry or report."""


class VkAuthenticationError(VkApiError):
    pass


class VkTransportError(VkApiError):
    pass


class VkDeliveryUnknown(VkApiError):
    """Transport failed during wall.post; automatic retry could duplicate it."""


@dataclass(frozen=True, slots=True)
class VkCommunityPreflight:
    group_id: int
    screen_name: str
    community_url: str
    readable: bool


@dataclass(frozen=True, slots=True)
class VkPostResult:
    post_id: int
    url: str


OpenUrl = Callable[..., Any]


class VkApiClient:
    API_BASE = "https://api.vk.com/method/"

    def __init__(
        self,
        token: str,
        *,
        api_version: str = "5.199",
        timeout: int = 30,
        opener: OpenUrl = urlopen,
    ) -> None:
        clean = token.strip()
        if len(clean) < 20 or any(char.isspace() for char in clean):
            raise VkAuthenticationError("VK_TOKEN_MISSING_OR_INVALID")
        self._token = clean
        self.api_version = api_version
        self.timeout = timeout
        self._opener = opener

    @staticmethod
    def _safe_api_error(payload: dict[str, Any]) -> VkApiError:
        error = payload.get("error") or {}
        code = int(error.get("error_code") or 0)
        safe = f"VK_API_ERROR_{code}: API rejected request"
        if code in {5, 15, 27, 28}:
            return VkAuthenticationError(safe)
        return VkApiError(safe)

    def call(self, method: str, **params: object) -> Any:
        payload = {
            key: ("1" if value is True else "0" if value is False else str(value))
            for key, value in params.items()
            if value is not None
        }
        payload["access_token"] = self._token
        payload["v"] = self.api_version
        request = Request(
            self.API_BASE + method,
            data=urlencode(payload).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            error = VkTransportError(f"VK_TRANSPORT_ERROR during {method}")
            if method == "wall.post":
                error = VkDeliveryUnknown("VK_DELIVERY_UNKNOWN during wall.post")
            raise error from None
        if not isinstance(result, dict):
            raise VkTransportError(f"VK_INVALID_RESPONSE during {method}")
        if "error" in result:
            raise self._safe_api_error(result)
        return result.get("response")

    def resolve_community(self, screen_name: str) -> VkCommunityPreflight:
        clean = screen_name.strip().lstrip("@")
        if not clean:
            raise ValueError("VK_SCREEN_NAME_MISSING")
        resolved = self.call("utils.resolveScreenName", screen_name=clean)
        if not isinstance(resolved, dict) or str(resolved.get("type")) != "group":
            raise VkApiError("VK_COMMUNITY_NOT_FOUND")
        group_id = int(resolved.get("object_id") or 0)
        if group_id <= 0:
            raise VkApiError("VK_COMMUNITY_ID_INVALID")
        wall = self.call("wall.get", owner_id=-group_id, count=1)
        if not isinstance(wall, dict):
            raise VkApiError("VK_COMMUNITY_WALL_UNREADABLE")
        return VkCommunityPreflight(
            group_id=group_id,
            screen_name=clean,
            community_url=f"https://vk.ru/{clean}",
            readable=True,
        )

    def _upload_wall_photo(self, group_id: int, image_path: Path) -> str:
        image = image_path.resolve()
        if not image.is_file():
            raise VkApiError("VK_IMAGE_FILE_MISSING")
        server = self.call("photos.getWallUploadServer", group_id=group_id)
        if not isinstance(server, dict) or not str(server.get("upload_url") or "").startswith(
            "https://"
        ):
            raise VkApiError("VK_UPLOAD_SERVER_INVALID")
        uploaded = self._multipart_upload(str(server["upload_url"]), image)
        required = ("photo", "server", "hash")
        if not isinstance(uploaded, dict) or any(key not in uploaded for key in required):
            raise VkApiError("VK_PHOTO_UPLOAD_INVALID_RESPONSE")
        saved = self.call(
            "photos.saveWallPhoto",
            group_id=group_id,
            photo=uploaded["photo"],
            server=uploaded["server"],
            hash=uploaded["hash"],
        )
        if not isinstance(saved, list) or not saved or not isinstance(saved[0], dict):
            raise VkApiError("VK_PHOTO_SAVE_INVALID_RESPONSE")
        owner_id = int(saved[0].get("owner_id") or 0)
        photo_id = int(saved[0].get("id") or 0)
        if not owner_id or not photo_id:
            raise VkApiError("VK_PHOTO_MAPPING_MISSING")
        return f"photo{owner_id}_{photo_id}"

    def _multipart_upload(self, upload_url: str, image_path: Path) -> dict[str, Any]:
        boundary = f"----PremWire{uuid4().hex}"
        mime = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; filename="{image_path.name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        body = head + image_path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("ascii")
        request = Request(
            upload_url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise VkTransportError("VK_PHOTO_UPLOAD_TRANSPORT_ERROR") from exc
        if not isinstance(result, dict):
            raise VkTransportError("VK_PHOTO_UPLOAD_INVALID_RESPONSE")
        return result

    def publish(
        self,
        *,
        group_id: int,
        text: str,
        image_path: Path | None,
        comments_enabled: bool,
        guid: str,
    ) -> VkPostResult:
        attachments = (
            self._upload_wall_photo(group_id, image_path) if image_path is not None else None
        )
        response = self.call(
            "wall.post",
            owner_id=-group_id,
            from_group=True,
            message=text.strip(),
            attachments=attachments,
            close_comments=not comments_enabled,
            guid=guid,
        )
        if not isinstance(response, dict) or int(response.get("post_id") or 0) <= 0:
            raise VkApiError("VK_POST_ID_MISSING")
        post_id = int(response["post_id"])
        return VkPostResult(post_id, f"https://vk.ru/wall-{group_id}_{post_id}")

    def edit(
        self,
        *,
        group_id: int,
        post_id: int,
        text: str,
        image_path: Path | None,
        comments_enabled: bool,
    ) -> None:
        attachments = (
            self._upload_wall_photo(group_id, image_path) if image_path is not None else None
        )
        self.call(
            "wall.edit",
            owner_id=-group_id,
            post_id=post_id,
            message=text.strip(),
            attachments=attachments,
            close_comments=not comments_enabled,
        )

    def delete(self, *, group_id: int, post_id: int) -> None:
        self.call("wall.delete", owner_id=-group_id, post_id=post_id)


__all__ = [
    "VkApiClient",
    "VkApiError",
    "VkAuthenticationError",
    "VkCommunityPreflight",
    "VkDeliveryUnknown",
    "VkPostResult",
    "VkTransportError",
]
