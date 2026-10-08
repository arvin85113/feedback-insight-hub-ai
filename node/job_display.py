"""Read-only presentation of existing job and publication metadata."""


def describe_job(job):
    from feedback.job_progress import EXECUTOR_PHASES, PHASES

    if job.cancel_requested_at and job.status in ("pending", "running"):
        job.ui_label, job.ui_tone = "取消中", "waiting"
    else:
        job.ui_label, job.ui_tone = {
            "pending": ("等待執行", "waiting"), "running": ("分析中", "running"),
            "succeeded": ("執行完成", "ready"), "failed": ("執行失敗", "failed"),
            "cancelled": ("已取消", "neutral"),
        }.get(job.status, ("待確認", "neutral"))
    job.ui_progress_known = job.status in ("pending", "succeeded")
    job.ui_progress_percent = 100 if job.status == "succeeded" else 0
    job.ui_progress_phase = {
        "pending": "等待背景 Worker 領取", "succeeded": "本機結果已發布（不代表已上傳）",
        "failed": "執行失敗，結果未發布", "cancelled": "工作已停止",
    }.get(job.status, "正在執行，等待 Worker 回報階段")
    job.ui_progress_units = ""
    checkpoint = (job.result_manifest or {}).get("_progress")
    if isinstance(checkpoint, dict) and job.status in ("running", "failed", "cancelled"):
        done = checkpoint.get("completed")
        if (checkpoint.get("version") == 1 and checkpoint.get("attempt") == job.attempt_count
                and checkpoint.get("phase") in EXECUTOR_PHASES.get(job.executor, set())
                and type(done) is int and 0 <= done < 4 and checkpoint.get("total") == 4):
            job.ui_progress_known = True
            job.ui_progress_percent = done * 25
            job.ui_progress_units = f"已完成 {done}/4 階段（非耗時預估）"
            if job.status == "running":
                job.ui_progress_phase = PHASES[checkpoint["phase"]]
    if job.cancel_requested_at and job.status in ("pending", "running"):
        job.ui_progress_phase = "取消要求已送出，等待目前步驟安全停止"
    job.ui_progress_busy = job.status == "running" and not job.cancel_requested_at
    return job


def describe_survey(survey, *, binding, state, active_jobs, latest_job, key_configured):
    def matches(job):
        return bool(binding and state and job.source_kind == binding.kind
                    and job.source_ref == binding.source_ref and job.source_version == binding.source_version
                    and job.input_version == state.input_version and job.config_version == state.config_version
                    and job.pipeline_version == state.pipeline_version)

    current = [job for job in active_jobs if job.executor == "deterministic" and matches(job)]
    current.sort(key=lambda job: (job.status != "running", job.pk))
    survey.live_job = current[0] if current else None
    ai_jobs = [job for job in active_jobs if job.executor == "ai" and matches(job)]
    ai_jobs.sort(key=lambda job: (job.status != "running", job.pk))
    survey.live_ai_job = ai_jobs[0] if ai_jobs else None
    survey.active_base_count = len(current)
    survey.has_published_base = bool(state and (state.publication_manifest or {}).get("statistics"))
    survey.ui_tone, survey.ui_label = "neutral", "尚未分析"
    survey.ui_hint = "資料已就緒，開始統計與文字分析後，結果會發布到本機。"
    if binding is None:
        survey.ui_tone, survey.ui_label = "failed", "來源需要處理"
        survey.ui_hint = "來源設定不完整，請至資料集頁確認版本與登錄。"
    elif survey.live_job:
        survey.ui_tone, survey.ui_label = survey.live_job.ui_tone, survey.live_job.ui_label
        survey.ui_hint = "背景工作正在執行；完成後才會切換已發布結果。" if survey.live_job.status == "running" else "工作已排入佇列，等待背景 Worker 領取。"
    elif not survey.reply_count:
        survey.ui_label, survey.ui_hint = "尚無資料", "收到回覆或登錄資料集後，才能開始分析。"
    elif survey.base_current:
        survey.ui_tone, survey.ui_label = "ready", "結果已更新"
        survey.ui_hint = "統計與文字結果符合目前資料版本，可查看結果或接著產生 AI 解讀。"
    elif latest_job and matches(latest_job) and latest_job.status == "failed":
        survey.ui_tone, survey.ui_label = "failed", "分析失敗"
        survey.ui_hint = "本次工作未完成；上一份成功結果仍保留。請先查看下方錯誤代碼。"
    elif survey.has_published_base:
        survey.ui_tone, survey.ui_label = "waiting", "結果待更新"
        survey.ui_hint = "目前顯示上一份已發布結果，需要重新分析目前版本。"
    survey.can_analyse = bool(binding and survey.reply_count and not current)
    survey.analyse_label = "分析進行中" if current else ("重新分析" if survey.has_published_base else "開始統計與文字分析")
    survey.ai_block_reason = ""
    if survey.live_ai_job:
        survey.ai_block_reason = "目前版本已有 Gemini 工作，請等待完成；不需要重新確認額度。"
    elif not key_configured:
        survey.ai_block_reason = "先設定 Gemini 金鑰；統計與文字分析不需要金鑰。"
    elif not survey.base_current:
        survey.ai_block_reason = "先完成目前版本的統計與文字分析，再預覽 AI 解讀。"
