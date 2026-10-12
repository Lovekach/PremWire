from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading

from .config import AppConfig
from .storage import Storage
from .vk_api import (
    VkApiClient,
    VkApiError,
    VkAuthenticationError,
    VkDeliveryUnknown,
    VkPostResult,
)


_VK_MIRROR_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class VkMirrorSummary:
    published: int = 0
    replaced: int = 0
    deleted: int = 0
    warnings: tuple[str, ...] = ()


def process_vk_mirroring(
    config: AppConfig,
    env: dict[str, str],
    storage: Storage,
    *,
    client: VkApiClient | None = None,
) -> VkMirrorSummary:
    settings = getattr(config, "vk_publishing", None)
    if settings is None or not settings.enabled:
        return VkMirrorSummary()
    if not _VK_MIRROR_LOCK.acquire(blocking=False):
        return VkMirrorSummary()
    try:
        return _process_vk_mirroring_locked(
            config, env, storage, client=client
        )
    finally:
        _VK_MIRROR_LOCK.release()


def _process_vk_mirroring_locked(
    config: AppConfig,
    env: dict[str, str],
    storage: Storage,
    *,
    client: VkApiClient | None,
) -> VkMirrorSummary:
    settings = config.vk_publishing
    if settings.group_id <= 0:
        return VkMirrorSummary(warnings=("VK_GROUP_ID_MISSING",))
    auth_block = storage.get_runtime_state(storage.VK_AUTH_BLOCK_KEY)
    if auth_block:
        return VkMirrorSummary()
    if client is None:
        token = env.get("VK_USER_TOKEN", "").strip()
        if not token:
            return VkMirrorSummary(warnings=("VK_USER_TOKEN_MISSING",))
        try:
            client = VkApiClient(token, api_version=settings.api_version)
        except VkApiError as exc:
            return VkMirrorSummary(warnings=(str(exc),))

    published = replaced = deleted = 0
    warnings: list[str] = []
    paused = storage.publication_is_paused() or getattr(getattr(config, "production", None), "mode", "continuous") == "daily_digest"
    for row in storage.vk_mirror_queue(limit=settings.max_per_run):
        item_id = int(row["id"])
        status = str(row["vk_status"] or "")
        if paused and status not in {"needs_delete", "delete_failed"}:
            continue
        try:
            if status in {"needs_delete", "delete_failed"}:
                post_id = int(row["vk_post_id"] or 0)
                if post_id:
                    client.delete(group_id=settings.group_id, post_id=post_id)
                storage.mark_vk_deleted(item_id)
                deleted += 1
                continue

            text = str(row["vk_text"] or "").strip()
            if not text:
                raise VkApiError("VK_TEXT_MISSING")
            raw_image = str(row["image_path"] or "").strip()
            image_path = Path(raw_image) if raw_image else None
            if image_path is not None and not image_path.is_file():
                image_path = None

            if status in {"needs_replace", "replace_failed"}:
                post_id = int(row["vk_post_id"] or 0)
                if post_id <= 0:
                    raise VkApiError("VK_REPLACE_MAPPING_MISSING")
                client.edit(
                    group_id=settings.group_id,
                    post_id=post_id,
                    text=text,
                    image_path=image_path,
                    comments_enabled=settings.comments_enabled,
                )
                storage.mark_vk_replaced(item_id, text)
                replaced += 1
                continue

            result: VkPostResult = client.publish(
                group_id=settings.group_id,
                text=text,
                image_path=image_path,
                comments_enabled=settings.comments_enabled,
                guid=f"premwire-item-{item_id}",
            )
            storage.mark_vk_published(item_id, result.post_id, result.url, text)
            published += 1
        except VkDeliveryUnknown as exc:
            storage.mark_vk_delivery_unknown(item_id, str(exc))
            warnings.append(f"VK item {item_id}: {exc}; manual reconciliation required")
        except VkAuthenticationError as exc:
            action = (
                "delete"
                if status in {"needs_delete", "delete_failed"}
                else "replace"
                if status in {"needs_replace", "replace_failed"}
                else "publish"
            )
            storage.mark_vk_auth_failed(item_id, action, str(exc))
            storage.set_runtime_state(storage.VK_AUTH_BLOCK_KEY, str(exc)[:1000])
            warnings.append(
                f"VK item {item_id}: {exc}; credential replacement required"
            )
            break
        except (VkApiError, ValueError) as exc:
            action = (
                "delete"
                if status in {"needs_delete", "delete_failed"}
                else "replace"
                if status in {"needs_replace", "replace_failed"}
                else "publish"
            )
            storage.mark_vk_failed(item_id, action, str(exc))
            warnings.append(f"VK item {item_id}: {exc}")
    return VkMirrorSummary(published, replaced, deleted, tuple(warnings))


__all__ = ["VkMirrorSummary", "process_vk_mirroring"]
