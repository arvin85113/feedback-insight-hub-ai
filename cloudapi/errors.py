class DefinitionCommitError(Exception):
    """A survey definition change that could not be committed; `user_message` is shown on the page."""

    user_message = "問卷儲存失敗"


class VersionConflict(DefinitionCommitError):
    user_message = "版本不一致，請重新載入"

    def __init__(self, current_version):
        super().__init__(f"current version is {current_version}")
        self.current_version = current_version


class SemanticLockViolation(DefinitionCommitError):
    user_message = "此題已有回覆，請新增題目取代並停用舊題"

    def __init__(self, question_uuid):
        super().__init__(f"question {question_uuid} already has answers")
        self.question_uuid = question_uuid


class DefinitionError(DefinitionCommitError, ValueError):
    user_message = "問卷內容無效"
