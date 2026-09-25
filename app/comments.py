"""Normalize supported VK comment events without mixing their ID namespaces."""

from dataclasses import dataclass


def integer(value):
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


@dataclass(frozen=True)
class Comment:
    source_type: str
    owner_id: int
    object_id: int
    comment_id: int
    user_id: int
    text: str

    @property
    def event_key(self):
        return f"{self.source_type}:{self.owner_id}:{self.object_id}:{self.comment_id}"


def normalize_comment(payload, group_id):
    event_type = payload.get("type")
    if event_type not in {"wall_reply_new", "video_comment_new"}:
        return None
    if group_id <= 0 or integer(payload.get("group_id")) != group_id:
        return None
    obj = payload.get("object")
    if not isinstance(obj, dict):
        return None
    source_type = "video" if event_type == "video_comment_new" else "wall"
    # VK's video event adds video_owner_id to a wall-comment-shaped object.
    owner_field = "video_owner_id" if source_type == "video" else "post_owner_id"
    owner_id = integer(obj.get(owner_field, obj.get("owner_id", -group_id)))
    object_id = integer(obj.get("video_id" if source_type == "video" else "post_id"))
    comment_id, user_id = integer(obj.get("id")), integer(obj.get("from_id"))
    if owner_id != -group_id or min(object_id, comment_id, user_id) <= 0:
        return None  # Ignore other owners and comments authored by communities.
    return Comment(
        source_type,
        owner_id,
        object_id,
        comment_id,
        user_id,
        str(obj.get("text") or "").strip(),
    )
