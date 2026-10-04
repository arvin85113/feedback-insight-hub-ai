import re
import uuid

from django import forms

from .models import ImprovementNotice, ImprovementUpdate, Question, Survey, SurveyCategory
from .question_schema import kind_display_for, normalize_question, question_errors


class SurveyFormBuilder(forms.Form):
    def __init__(self, *args, survey: Survey, **kwargs):
        kwargs.setdefault("label_suffix", "")  # question titles end with their own punctuation
        super().__init__(*args, **kwargs)
        self.survey = survey
        for question in survey.questions.filter(is_active=True):
            self.fields[f"question_{question.id}"] = self._build_field(question)

    def _build_field(self, question: Question):
        common = {
            "label": question.title,
            "required": question.is_required,
            "help_text": question.help_text,
        }
        if question.kind == Question.Kind.SHORT_TEXT:
            return forms.CharField(max_length=255, **common)
        if question.kind == Question.Kind.LONG_TEXT:
            return forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), **common)
        if question.kind == Question.Kind.SINGLE_CHOICE:
            choices = [(choice["code"], choice["label"]) for choice in question.choices]
            if question.display == "dropdown":
                return forms.ChoiceField(choices=[("", "請選擇")] + choices, widget=forms.Select, **common)
            return forms.ChoiceField(choices=choices, widget=forms.RadioSelect, **common)
        if question.kind == Question.Kind.MULTIPLE_CHOICE:
            return forms.MultipleChoiceField(
                choices=[(choice["code"], choice["label"]) for choice in question.choices],
                widget=forms.CheckboxSelectMultiple,
                **common,
            )
        if question.kind == Question.Kind.INTEGER:
            return forms.IntegerField(**common)
        if question.kind == Question.Kind.DECIMAL:
            return forms.DecimalField(decimal_places=2, max_digits=10, **common)
        if question.kind == Question.Kind.SCALE:
            values = question.analysis_options or [str(v) for v in range(1, 6)]
            field = forms.ChoiceField(
                choices=[(value, value) for value in values],
                widget=forms.RadioSelect(attrs={"class": "scale-option"}),
                **common,
            )
            # Rendered at the two ends of the scale buttons (builder spec §1).
            field.scale_min_label = question.scale_min_label
            field.scale_max_label = question.scale_max_label
            field.is_scale = True
            return field
        return forms.CharField(**common)


class RespondentMetaForm(forms.Form):
    idempotency_key = forms.UUIDField(
        widget=forms.HiddenInput,
        initial=uuid.uuid4,
        required=False,
    )
    consent_follow_up = forms.BooleanField(label="願意接收後續改善通知", required=False)


class ImprovementUpdateForm(forms.ModelForm):
    class Meta:
        model = ImprovementUpdate
        fields = ("title", "summary", "related_category")
        labels = {
            "title": "改善主題",
            "summary": "改善摘要",
            "related_category": "對應分類",
        }


class ImprovementEditForm(forms.ModelForm):
    class Meta:
        model = ImprovementUpdate
        fields = (
            "title",
            "summary",
            "related_category",
            "priority",
            "due_date",
            "internal_note",
        )
        labels = {
            "title": "改善主題",
            "summary": "改善摘要",
            "related_category": "對應分類",
            "priority": "優先程度",
            "due_date": "預計完成日",
            "internal_note": "內部備註",
        }
        widgets = {
            "summary": forms.Textarea(attrs={"rows": 7}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "internal_note": forms.Textarea(attrs={"rows": 5}),
        }


class ImprovementStatusTransitionForm(forms.Form):
    status = forms.ChoiceField(label="下一狀態")

    def __init__(self, *args, improvement, choices, **kwargs):
        super().__init__(*args, **kwargs)
        self.improvement = improvement
        self.fields["status"].choices = choices


class ImprovementNoticeForm(forms.ModelForm):
    def __init__(self, *args, improvement, **kwargs):
        super().__init__(*args, **kwargs)
        self.improvement = improvement
        if improvement.survey_id is None:
            self.fields["audience_type"].choices = [
                choice
                for choice in ImprovementNotice.AudienceType.choices
                if choice[0] == ImprovementNotice.AudienceType.GLOBAL
            ]

    def clean_audience_type(self):
        audience_type = self.cleaned_data["audience_type"]
        if (
            audience_type == ImprovementNotice.AudienceType.SURVEY_RESPONDENTS
            and self.improvement.survey_id is None
        ):
            raise forms.ValidationError("來源問卷已移除，無法選擇問卷填答者。")
        return audience_type

    class Meta:
        model = ImprovementNotice
        fields = ("subject", "body", "audience_type")
        labels = {
            "subject": "通知主旨",
            "body": "通知內容",
            "audience_type": "通知對象",
        }
        widgets = {
            "body": forms.Textarea(attrs={"rows": 9}),
        }


class ImprovementNoticeConfirmationForm(forms.Form):
    confirmation_token = forms.UUIDField(widget=forms.HiddenInput)
    content_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class SurveyCreateForm(forms.ModelForm):
    category = forms.ModelChoiceField(
        queryset=SurveyCategory.objects.all(),
        required=False,
        empty_label="── 選擇分類（選填）──",
        label="問卷分類",
        widget=forms.Select(),
    )

    class Meta:
        model = Survey
        fields = (
            "title",
            "category",
            "description",
            "thank_you_email_enabled",
            "analysis_enabled",
        )
        labels = {
            "title": "問卷名稱",
            "category": "問卷分類",
            "description": "問卷說明",
            "thank_you_email_enabled": "完成後寄送確認信",
            "analysis_enabled": "自動分析",
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }


UI_TYPE_LABELS = (
    ("short_text", "簡答"),
    ("long_text", "段落"),
    ("radio", "選擇題"),
    ("dropdown", "下拉選單"),
    ("checkbox", "核取方塊"),
    ("scale", "線性刻度"),
    ("number", "數字"),
)
_CHOICE_KEY = re.compile(r"^choices-(\d+)-label$")


class QuestionCardForm(forms.Form):
    """One question card of the builder (spec §3); the server derives kind, display and data type."""

    question_uuid = forms.CharField(required=False, widget=forms.HiddenInput)
    ui_type = forms.ChoiceField(choices=UI_TYPE_LABELS, label="題型", error_messages={"required": "請選擇題型"})
    title = forms.CharField(max_length=255, label="題目")
    help_text = forms.CharField(max_length=255, required=False, label="說明")
    is_required = forms.BooleanField(required=False, label="必填")
    enable_keyword_tracking = forms.BooleanField(required=False, label="納入文字分析")
    ordered = forms.BooleanField(required=False, label="選項有高低順序")
    score_start = forms.TypedChoiceField(choices=((1, "1"), (0, "0")), coerce=int, required=False, empty_value=1,
                                         label="分數起點")
    allow_decimal = forms.BooleanField(required=False, label="允許小數")
    scale_min = forms.TypedChoiceField(choices=((1, "1"), (0, "0")), coerce=int, required=False, empty_value=1,
                                       label="起點")
    scale_max = forms.TypedChoiceField(choices=[(n, str(n)) for n in range(2, 11)], coerce=int, required=False,
                                       empty_value=5, label="終點")
    scale_min_label = forms.CharField(max_length=40, required=False, label="起點標籤")
    scale_max_label = forms.CharField(max_length=40, required=False, label="終點標籤")

    def __init__(self, data=None, *args, **kwargs):
        super().__init__(data, *args, **kwargs)
        self.rows = self._rows(data) if data is not None else []

    @staticmethod
    def _rows(data):
        """Each option row arrives as one complete group of fields; order by position, drop blank labels."""

        rows = []
        for key in data:
            match = _CHOICE_KEY.match(key)
            if not match:
                continue
            index = match.group(1)
            try:
                position = int(data.get(f"choices-{index}-position", index))
            except ValueError:
                position = int(index)
            excluded_values = data.getlist(f"choices-{index}-excluded") if hasattr(data, "getlist") else []
            rows.append({
                "position": position,
                "code": data.get(f"choices-{index}-code", "").strip(),
                "label": data.get(key, ""),
                "excluded": bool(excluded_values) and excluded_values[-1] == "1",
            })
        rows.sort(key=lambda row: row["position"])
        return [row for row in rows if row["label"].strip()]

    def to_item(self):
        data = self.cleaned_data
        kind, display = kind_display_for(data["ui_type"], allow_decimal=data.get("allow_decimal", False))
        return {
            "title": data["title"],
            "help_text": data.get("help_text", ""),
            "kind": kind,
            "display": display,
            "ordered": data.get("ordered", False),
            "score_start": data.get("score_start", 1),
            "scale_min": data.get("scale_min", 1),
            "scale_max": data.get("scale_max", 5),
            "scale_min_label": data.get("scale_min_label", ""),
            "scale_max_label": data.get("scale_max_label", ""),
            "choices": [{"code": row["code"], "label": row["label"], "excluded": row["excluded"], "score": None}
                        for row in self.rows],
            "is_required": data.get("is_required", False),
            "enable_keyword_tracking": data.get("enable_keyword_tracking", False),
        }

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        item = normalize_question({**self.to_item(), "next_choice_number": 1, "data_type": "", "options_text": ""})
        for field, message in question_errors(item).items():
            self.add_error(field if field in self.fields else None, message)
        return cleaned


class SurveyEditForm(forms.ModelForm):
    category = forms.ModelChoiceField(
        queryset=SurveyCategory.objects.all(),
        required=False,
        empty_label="── 選擇分類（選填）──",
        label="問卷分類",
        widget=forms.Select(),
    )

    class Meta:
        model = Survey
        fields = (
            "title",
            "category",
            "description",
            "is_active",
            "analysis_enabled",
            "thank_you_email_enabled",
        )
        labels = {
            "title": "問卷名稱",
            "category": "問卷分類",
            "description": "問卷說明",
            "is_active": "收件中",
            "analysis_enabled": "自動分析",
            "thank_you_email_enabled": "完成後寄送確認信",
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }
