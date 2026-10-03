class DefinitionCommitError(Exception):
    """A survey definition change that could not be committed; `user_message` is shown on the page."""

    user_message = "問卷儲存失敗"


class VersionConflict(DefinitionCommitError):
    user_message = "版本不一致，請重新載入"

    def __init__(self, current_version):
        super().__init__(f"current version is {current_version}")
        self.current_version = current_version


class PublishedLocked(DefinitionCommitError):
    """A published survey's questions, title and description never change (builder spec §4.2)."""

    code = "published_locked"
    user_message = "問卷已發布，題目不能修改；請複製為新草稿"


class PublishBlocked(DefinitionCommitError):
    """A node-owned survey can be published only once its replies can go to the inbox (spec §4.3)."""

    code = "publish_blocked"
    user_message = "收件匣尚未開啟，無法發布指派節點的問卷"


class DefinitionError(DefinitionCommitError, ValueError):
    user_message = "問卷內容無效"
