from django import forms

from .models import ImprovementNotice, ImprovementUpdate, Question, Survey, SurveyCategory


class SurveyFormBuilder(forms.Form):
    def __init__(self, *args, survey: Survey, **kwargs):
        super().__init__(*args, **kwargs)
        self.survey = survey
        for question in survey.questions.all():
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
            return forms.ChoiceField(
                choices=[(option, option) for option in question.options],
                widget=forms.RadioSelect,
                **common,
            )
        if question.kind == Question.Kind.MULTIPLE_CHOICE:
            return forms.MultipleChoiceField(
                choices=[(option, option) for option in question.options],
                widget=forms.CheckboxSelectMultiple,
                **common,
            )
        if question.kind == Question.Kind.INTEGER:
            return forms.IntegerField(**common)
        if question.kind == Question.Kind.DECIMAL:
            return forms.DecimalField(decimal_places=2, max_digits=10, **common)
        if question.kind == Question.Kind.SCALE:
            options = question.options
            if options:
                return forms.ChoiceField(
                    choices=[(o, o) for o in options],
                    widget=forms.RadioSelect,
                    **common,
                )
            return forms.IntegerField(min_value=1, max_value=5, **common)
        return forms.CharField(**common)


class RespondentMetaForm(forms.Form):
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
            "is_active",
        )
        labels = {
            "title": "問卷名稱",
            "category": "問卷分類",
            "description": "問卷說明",
            "thank_you_email_enabled": "完成後寄送確認信",
            "is_active": "立即啟用問卷",
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }


class QuestionCreateForm(forms.ModelForm):
    class Meta:
        model = Question
        fields = (
            "title",
            "help_text",
            "kind",
            "data_type",
            "options_text",
            "is_required",
            "enable_keyword_tracking",
            "order",
        )
        labels = {
            "title": "題目名稱",
            "help_text": "補充說明",
            "kind": "作答形式",
            "data_type": "資料型態",
            "options_text": "選項內容",
            "is_required": "必填",
            "enable_keyword_tracking": "納入文字關鍵字分析",
            "order": "排序",
        }
        widgets = {
            "options_text": forms.Textarea(
                attrs={
                    "rows": 5,
                    "placeholder": "每行一個選項\n例如：\n非常滿意\n滿意\n普通\n不滿意",
                }
            ),
            "help_text": forms.TextInput(attrs={"placeholder": "例如：請依照最近一次使用經驗作答"}),
        }


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
        fields = ("title", "category", "description", "is_active", "thank_you_email_enabled")
        labels = {
            "title": "問卷名稱",
            "category": "問卷分類",
            "description": "問卷說明",
            "is_active": "立即啟用問卷",
            "thank_you_email_enabled": "完成後寄送確認信",
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }
