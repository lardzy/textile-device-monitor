"""Historical v1 human protocols; native forms and selection use NodeSpec contracts."""


def normalize_v1_submission(db, *, run, node_run, data, task=None, actor=None):
    # Lazy imports retain the established helpers and avoid loading this path for native Runs.
    from app.execution.engine import (
        ExecutionApiError,
        _assert_declared_root_refs,
        _candidate_items,
        _compact_text_options,
        _definition_node_map,
        _task_project_conditions,
        _truthy_judgement_flag,
        _validate_index_candidate,
        build_file_gateway,
        cached_task_snapshot,
        conflict,
        image_selection_counts,
        microscopy_family_from_config,
        microscopy_rule_key,
        normalize_report_image_placement_submission,
        resolve_rule,
    )

    if node_run.node_type == "human.image_selection":
        selected_ids = data.get("selected_image_ids")
        if not isinstance(selected_ids, list) or not 1 <= len(selected_ids) <= 10:
            raise ExecutionApiError(
                422,
                "image_selection_count_invalid",
                "请选择 1 至 10 张图片",
            )
        supported_counts = image_selection_counts(run.definition_snapshot, node_run.node_id)
        if supported_counts and len({str(value) for value in selected_ids}) not in supported_counts:
            raise ExecutionApiError(
                422, "microscopy_template_image_count_unsupported",
                "当前记录模板支持 " + "、".join(map(str, supported_counts)) + " 张图片，请调整选图数量",
                details={"supported_image_counts": supported_counts},
            )
        offered_images = node_run.input_data.get("images") or []
        images_by_id = {
            str(item.get("id")): item
            for item in offered_images
            if isinstance(item, dict) and item.get("id")
        }
        folder_ids = (
            data.get("selected_folder_ids")
            or node_run.input_data.get("selected_folder_ids")
            or []
        )
        if not isinstance(folder_ids, list):
            raise ExecutionApiError(
                422, "image_folder_selection_invalid", "图片目录选择格式无效"
            )
        selected_folder_ids = list(dict.fromkeys(str(value) for value in folder_ids))
        offered_folder_ids = {
            str(item.get("id"))
            for item in (node_run.input_data.get("folders") or [])
            if isinstance(item, dict) and item.get("id")
        }
        if any(value not in offered_folder_ids for value in selected_folder_ids):
            raise ExecutionApiError(
                409,
                "image_folder_not_offered",
                "所选图片目录不在当前候选列表中",
            )
        if node_run.input_data.get("folder_selection_required") and not selected_folder_ids:
            raise ExecutionApiError(
                422, "image_folder_selection_required", "请先选择图片所在目录"
            )
        normalized_images: list[dict[str, Any]] = []
        seen: set[str] = set()
        gateway = build_file_gateway(db)
        for value in selected_ids:
            image_id = str(value)
            if image_id in seen:
                continue
            candidate = images_by_id.get(image_id)
            if candidate is None:
                raise ExecutionApiError(
                    409,
                    "image_candidate_not_offered",
                    "所选图片不在当前候选列表中",
                )
            if (
                selected_folder_ids
                and candidate.get("folder_id") not in selected_folder_ids
            ):
                raise ExecutionApiError(
                    422,
                    "image_candidate_outside_selected_folders",
                    "所选图片不属于已选择的目录",
                )
            _validate_index_candidate(
                db, run=run, candidate=candidate, gateway=gateway
            )
            normalized_images.append(candidate)
            seen.add(image_id)
        if not 1 <= len(normalized_images) <= 10:
            raise ExecutionApiError(
                422,
                "image_selection_count_invalid",
                "去除重复项后，请选择 1 至 10 张图片",
            )
        primary_image_id = data.get("primary_image_id")
        if primary_image_id is not None:
            primary_image_id = str(primary_image_id)
        elif len(normalized_images) == 1:
            primary_image_id = str(normalized_images[0]["id"])
        primary_image = next(
            (
                item
                for item in normalized_images
                if str(item.get("id")) == primary_image_id
            ),
            None,
        )
        if primary_image_id and primary_image is None:
            raise ExecutionApiError(
                422,
                "primary_image_not_selected",
                "主图必须是本次已选择的图片之一",
            )
        task_context: dict[str, Any] = {}
        if "task_validation_state" in (node_run.input_data or {}):
            task_snapshot = node_run.input_data.get("task")
            cache_state = node_run.input_data.get("task_cache_state")
            if not isinstance(task_snapshot, dict) or not task_snapshot:
                cached = cached_task_snapshot(
                    db,
                    inspection_number=run.inspection_number,
                )
                task_snapshot = cached.get("snapshot")
                cache_state = cached.get("cache_state")
            if not isinstance(task_snapshot, dict) or not task_snapshot:
                raise conflict(
                    "microscopy_task_snapshot_not_ready",
                    (
                        "旧系统任务信息尚未读取完成；图片选择会保留在当前页面，"
                        "请启动或检查 Windows 只读读取服务后重试提交"
                    ),
                    cache_state=cache_state or "pending",
                )
            matched_task_conditions = _task_project_conditions(
                task_snapshot,
                task_facts=resolve_rule(
                    db,
                    microscopy_rule_key(
                        microscopy_family_from_config(
                            {
                                "record_family": node_run.input_data.get(
                                    "record_family"
                                )
                            }
                        ).key
                    ),
                ).task_facts,
            )
            missing_task_conditions = [
                item
                for item in ("task_item_name", "test_method")
                if item not in matched_task_conditions
            ]
            task_context = {
                "task": task_snapshot,
                "task_cache_state": cache_state or "ready",
                "task_validation_state": (
                    "matched" if not missing_task_conditions else "warning"
                ),
                "missing_conditions": missing_task_conditions,
            }
        return {
            **data,
            "selected_folder_ids": selected_folder_ids,
            "selected_image_ids": [str(item["id"]) for item in normalized_images],
            "selected_images": normalized_images,
            "primary_image_id": primary_image_id,
            "primary_image": primary_image,
            **task_context,
        }
    task_kind = str(
        node_run.input_data.get("task_kind")
        or (node_run.input_data.get("record_context") or {}).get("task_kind")
        or ""
    )
    if task_kind == "microscopy_record_input":
        context = node_run.input_data.get("record_context") or node_run.input_data
        from app.execution.record_input import validate_microscopy_input

        return validate_microscopy_input(context, data)
    if task_kind == "microscopy_print_confirmation":
        artifact = node_run.input_data.get("artifact") or {}
        expected_sha = str(
            artifact.get("sha256") or artifact.get("content_sha256") or ""
        ).strip().casefold()
        submitted_sha = str(data.get("artifact_sha256") or "").strip().casefold()
        print_decision = str(data.get("print_decision") or "").strip().casefold()
        # Compatibility for human tasks created by the previously published
        # contract.  New tasks submit an explicit decision; an old
        # ``printed=true`` acknowledgement already meant printing was complete.
        legacy_print_confirmation = bool(
            not print_decision and data.get("printed") is True
        )
        if legacy_print_confirmation:
            print_decision = "print"
        if print_decision not in {"print", "skip"}:
            raise ExecutionApiError(
                422,
                "microscopy_print_confirmation_required",
                "请选择打印原始记录或暂不打印",
            )
        if not expected_sha or submitted_sha != expected_sha:
            raise ExecutionApiError(
                409,
                "microscopy_print_artifact_changed",
                "待打印原始记录已变化，请重新打开并核对",
            )
        print_requested = print_decision == "print"
        print_completed = bool(
            legacy_print_confirmation or data.get("print_completed") is True
        )
        if print_requested and not print_completed:
            raise ExecutionApiError(
                422,
                "microscopy_print_not_completed",
                "请确认已在 Excel 中完成打印",
            )
        return {
            "print_decision": print_decision,
            "print_requested": print_requested,
            "print_completed": print_requested and print_completed,
            "printed": print_requested and print_completed,
            "artifact_sha256": expected_sha,
            "artifact": artifact,
        }
    if node_run.node_type != "human.file_selection":
        _assert_declared_root_refs(run, data)
        if node_run.node_type == "human.input":
            node = _definition_node_map(run).get(node_run.node_id) or {}
            config = node.get("config") or {}
            if config.get("paper_existing_record_decision") or config.get(
                "legacy_existing_record_decision"
            ):
                selected_project = node_run.input_data.get("selected_project")
                task = node_run.input_data.get("task")
                if not isinstance(selected_project, dict) or not isinstance(
                    task, dict
                ):
                    raise ExecutionApiError(
                        409,
                        "paper_registration_context_changed",
                        "检验记录登记上下文已变化，请重新运行流程",
                    )
                check_count = selected_project.get("check_count")
                register_count = selected_project.get("register_count")
                if (
                    not isinstance(check_count, int)
                    or isinstance(check_count, bool)
                    or check_count < 1
                    or not isinstance(register_count, int)
                    or isinstance(register_count, bool)
                    or register_count < 0
                ):
                    raise ExecutionApiError(
                        409,
                        "paper_registration_count_invalid",
                        "旧系统返回的检测份数或已有登记数量无效，请刷新后重试",
                    )
                confirmation_required = check_count == 1 and register_count > 0
                action = str(
                    data.get("existing_record_action") or ""
                ).strip()
                if confirmation_required and action not in {"append", "cancel"}:
                    raise ExecutionApiError(
                        422,
                        "paper_existing_record_decision_required",
                        "当前项目已有登记，请选择直接新增或取消",
                    )
                if not confirmation_required:
                    action = "continue"
                return {
                    "existing_record_action": action,
                    "registration_cancelled": action == "cancel",
                    "expected_existing_register_count": register_count,
                    "selected_project": selected_project,
                    "selected_project_key": selected_project.get("project_key"),
                    "task": task,
                }
            if config.get("paper_judgement") or config.get(
                "legacy_generic_record_input"
            ):
                selected_project = node_run.input_data.get("selected_project")
                if not isinstance(selected_project, dict):
                    raise ExecutionApiError(
                        409,
                        "paper_registration_context_changed",
                        "纸浆项目登记上下文已变化，请重新运行流程",
                    )
                identities = _compact_text_options(
                    selected_project.get("sample_identify")
                )
                sample_identity = " ".join(
                    str(data.get("sample_identity") or "").strip().split()
                )
                if identities:
                    if not sample_identity:
                        raise ExecutionApiError(
                            422,
                            "paper_sample_identity_required",
                            "请确认当前录入的样品识别",
                        )
                    if sample_identity not in identities:
                        raise ExecutionApiError(
                            409,
                            "paper_sample_identity_not_offered",
                            "填写的样品识别不在任务单列表中，无法对应旧系统下拉框",
                        )
                elif sample_identity:
                    raise ExecutionApiError(
                        409,
                        "paper_sample_identity_not_offered",
                        "任务单未提供样品识别，不能写入旧系统下拉框",
                    )

                judgement_required = _truthy_judgement_flag(
                    selected_project.get("give_judgement")
                )
                judge_basis = " ".join(
                    str(data.get("judge_basis") or "").strip().split()
                )
                judgement = " ".join(
                    str(data.get("judgement") or "").strip().split()
                )
                standard_value = " ".join(
                    str(data.get("standard_value") or "").strip().split()
                )
                standard_value_required = bool(
                    config.get(
                        "require_standard_value",
                        config.get("paper_judgement") is True,
                    )
                )
                if (
                    judgement_required
                    and standard_value_required
                    and not standard_value
                ):
                    raise ExecutionApiError(
                        422,
                        "paper_standard_value_required",
                        "请填写标准值与允差",
                    )
                if judgement_required and (not judge_basis or not judgement):
                    raise ExecutionApiError(
                        422,
                        "paper_judgement_required",
                        "请确认判定依据与判定结果",
                    )
                if not judgement_required:
                    judge_basis = ""
                    judgement = ""
                    standard_value = ""
                check_count = selected_project.get("check_count")
                identity_count_mismatch = bool(
                    identities
                    and isinstance(check_count, int)
                    and not isinstance(check_count, bool)
                    and len(identities) != check_count
                )
                return {
                    "judgement_required": judgement_required,
                    "judge_basis": judge_basis,
                    "judgement": judgement,
                    "standard_value": standard_value,
                    "sample_identity": sample_identity or None,
                    "sample_identity_options": identities,
                    "identity_count_mismatch": identity_count_mismatch,
                }
            if config.get("report_image_placement"):
                return normalize_report_image_placement_submission(
                    db,
                    run=run,
                    node_run=node_run,
                    node=node,
                    data=data,
                )
        return data
    selected = data.get("selected_files")
    if not isinstance(selected, list) or not selected:
        raise ExecutionApiError(
            422,
            "file_selection_required",
            "请至少选择一个候选文件或采集组",
        )
    candidates = _candidate_items(node_run.input_data or {})
    candidates_by_id = {
        str(candidate["id"]): candidate
        for candidate in candidates
        if candidate.get("id")
    }
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    gateway = build_file_gateway(db)
    for submitted in selected:
        candidate_id = (
            submitted
            if isinstance(submitted, str)
            else submitted.get("id")
            if isinstance(submitted, dict)
            else None
        )
        candidate = candidates_by_id.get(str(candidate_id))
        if candidate is None:
            raise ExecutionApiError(
                409,
                "file_candidate_not_offered",
                "所选文件不在该人工任务的候选列表中",
            )
        if candidate.get("read_status") == "failed":
            raise ExecutionApiError(
                422,
                "result_file_not_selectable",
                "结果读取失败的文件不能被选用",
                details={"candidate_id": str(candidate_id)},
            )
        if str(candidate_id) in seen:
            continue
        _validate_index_candidate(
            db,
            run=run,
            candidate=candidate,
            gateway=gateway,
        )
        normalized.append(candidate)
        seen.add(str(candidate_id))

    node = _definition_node_map(run).get(node_run.node_id) or {}
    config = node.get("config") or {}
    if config.get("allow_multiple") is False and len(normalized) > 1:
        raise ExecutionApiError(
            422,
            "file_selection_multiple_not_allowed",
            "当前步骤只能选择一份文件",
        )
    require_primary = bool(config.get("require_primary"))
    primary_file_id = data.get("primary_file_id")
    if primary_file_id is not None:
        primary_file_id = str(primary_file_id)
    if primary_file_id is None and require_primary and len(normalized) == 1:
        primary_file_id = str(normalized[0]["id"])
    if require_primary and not primary_file_id:
        raise ExecutionApiError(
            422,
            "primary_file_required",
            "选择多个文件时，请指定其中一个文件作为主单",
        )
    primary_file = None
    if primary_file_id:
        primary_file = next(
            (
                candidate
                for candidate in normalized
                if str(candidate.get("id")) == primary_file_id
            ),
            None,
        )
        if primary_file is None:
            raise ExecutionApiError(
                422,
                "primary_file_not_selected",
                "主单必须是本次已选择的文件之一",
                details={"primary_file_id": primary_file_id},
            )
    return {
        **data,
        "selected_files": normalized,
        "primary_file_id": primary_file_id,
        "primary_file": primary_file,
    }

