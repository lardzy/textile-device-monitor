import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Select,
  Skeleton,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  ApartmentOutlined,
  ArrowRightOutlined,
  CheckCircleFilled,
  ClockCircleOutlined,
  EditOutlined,
  FileSearchOutlined,
  LoadingOutlined,
  LockOutlined,
  ReloadOutlined,
  SearchOutlined,
  StopOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import {
  getExecutionCatalogRecommendations,
  getExecutionCategories,
  getExecutionWorkflows,
} from '../../api/execution';
import { useExecutionAuth } from './ExecutionAuthContext';
import ExecutionChrome from './ExecutionChrome';
import './execution.css';

const { Paragraph, Text, Title } = Typography;

const getCategory = (workflow) => {
  if (workflow.category && typeof workflow.category === 'object') {
    return workflow.category;
  }
  return {
    id: workflow.category_id || workflow.category || 'uncategorized',
    key: workflow.category_key,
    name: workflow.category_name || workflow.category || '未分类',
  };
};

const categoryKeyOf = category => String(
  category?.key || category?.id || category?.slug || category?.name || 'uncategorized',
);

const workflowIdOf = workflow => workflow.id || workflow.workflow_id || workflow.slug;

const publishedVersionOf = workflow => workflow.published_version?.version
  || workflow.published_version
  || workflow.version;

const availabilityOf = (workflow) => {
  if (!publishedVersionOf(workflow)) {
    return { available: false, message: '流程尚未发布' };
  }
  const available = workflow.runnable !== false
    && workflow.available !== false
    && workflow.availability?.available !== false
    && workflow.root_status !== 'missing'
    && workflow.data_root_status !== 'missing'
    && workflow.is_enabled !== false;
  return {
    available,
    message: workflow.unavailable_reason
      || workflow.availability?.message
      || (workflow.is_enabled === false ? '流程已停用' : '所需数据根或运行能力尚未配置'),
  };
};

const runStatusMeta = {
  running: { color: 'processing', text: '最近运行中' },
  waiting_human: { color: 'warning', text: '等待人工处理' },
  waiting_external: { color: 'warning', text: '等待旧系统处理' },
  completed: { color: 'success', text: '最近已完成' },
  succeeded: { color: 'success', text: '最近已完成' },
  failed: { color: 'error', text: '最近失败' },
  cancel_pending: { color: 'warning', text: '正在安全取消' },
  failure_pending: { color: 'error', text: '失败收尾中' },
  cancelled: { color: 'default', text: '最近已取消' },
};

const recommendationConditionLabels = {
  category: '优先类别',
  source_root: '数据根',
  filename: '文件名',
  folder: '结果文件夹',
  worksheet: '工作表',
  content_range: '内容范围',
  task_item_name: '旧系统检测项目',
  test_method: '测试方法',
};

const recommendationDisplay = (recommendation, loading) => {
  if (loading) {
    return {
      color: 'processing',
      icon: <LoadingOutlined spin />,
      text: '正在识别',
      detail: '正在根据编号和优先类别识别适用流程',
    };
  }
  if (!recommendation) {
    return null;
  }
  if (
    (recommendation.index_state && recommendation.index_state !== 'ready')
    || ['index_unavailable', 'index_pending'].includes(recommendation.state)
  ) {
    return {
      color: 'warning',
      icon: <ClockCircleOutlined />,
      text: '索引暂不可用',
      detail: recommendation.message || '文件索引尚未就绪，您仍可手动选择并运行流程',
    };
  }
  const matchedConditions = recommendation.matched_conditions || [];
  const conditionText = matchedConditions.length
    ? matchedConditions
      .map(condition => recommendationConditionLabels[condition] || condition)
      .join('、')
    : '暂无';
  const score = Number(recommendation.score || 0);
  if (recommendation.full_match && Number(recommendation.candidate_count || 0) > 0) {
    const candidateCount = Number(recommendation.candidate_count);
    return {
      color: 'success',
      icon: <CheckCircleFilled />,
      text: `找到 ${candidateCount} 个符合文件`,
      detail: `已满足完整文件规则；匹配条件：${conditionText}`,
    };
  }
  return {
    color: score > 0 ? 'blue' : 'default',
    icon: score > 0 ? <FileSearchOutlined /> : <SearchOutlined />,
    text: score > 0 ? `匹配 ${score} 项` : '暂未匹配',
    detail: score > 0
      ? `匹配条件：${conditionText}`
      : '当前编号和优先类别暂未匹配此流程',
  };
};

function WorkflowCard({
  workflow,
  canRun,
  onRun,
  recommendation,
  recommendationLoading,
}) {
  const category = getCategory(workflow);
  const workflowId = workflowIdOf(workflow);
  const availability = availabilityOf(workflow);
  const { available } = availability;
  const actionable = available && canRun;
  const version = publishedVersionOf(workflow);
  const requiredCount = workflow.required_input_count
    ?? workflow.input_schema?.required?.length
    ?? workflow.published_definition?.input_schema?.required?.length
    ?? 0;
  const lastRun = workflow.last_run || workflow.latest_run;
  const lastStatus = runStatusMeta[lastRun?.status];
  const capabilities = workflow.capabilities || {};
  const canWrite = (Array.isArray(capabilities)
    ? capabilities.includes('write')
    : capabilities.write === true)
    || workflow.write_enabled
    || workflow.access_mode === 'write';
  const candidatePreview = recommendation?.candidate_preview;
  const candidatePreviewName = candidatePreview?.name
    || candidatePreview?.filename;
  const recommendationMeta = recommendationDisplay(
    recommendation,
    recommendationLoading,
  );

  return (
    <Card
      id={`execution-workflow-${workflowId}`}
      data-workflow-id={workflowId}
      className={`execution-workflow-card ${available ? '' : 'is-unavailable'}`}
      hoverable={actionable}
      onClick={() => actionable && onRun(workflow)}
      actions={[
        <Tooltip
          key="run"
          title={!canRun && available ? '当前账号没有运行流程权限' : undefined}
        >
          <Button
            type="link"
            disabled={!actionable}
            onClick={(event) => {
              event.stopPropagation();
              if (actionable) {
                onRun(workflow);
              }
            }}
          >
            {available ? '开始执行' : '暂不可运行'} <ArrowRightOutlined />
          </Button>
        </Tooltip>,
      ]}
    >
      <div className="execution-workflow-card__top">
        <span className="execution-workflow-card__icon">
          {available ? <ApartmentOutlined /> : <StopOutlined />}
        </span>
        <Space size={6} wrap>
          <Tag color="geekblue">{category.name}</Tag>
          <Tag icon={canWrite ? <EditOutlined /> : <LockOutlined />}>
            {canWrite ? '受控写入' : '只读'}
          </Tag>
        </Space>
      </div>
      <Title level={4}>{workflow.name || workflow.title}</Title>
      <Paragraph className="execution-workflow-card__description">
        {workflow.description || '按已发布流程处理检测资料并保留完整执行记录。'}
      </Paragraph>
      <div className="execution-workflow-card__meta">
        <Tooltip
          title={version
            ? `当前发布版本：v${version}`
            : '当前没有可执行的已发布版本'}
        >
          <span>
            {version ? <CheckCircleFilled /> : <StopOutlined />}
            {' '}{version ? '已发布' : '尚未发布'}
          </span>
        </Tooltip>
        <span><FileSearchOutlined /> {requiredCount} 项必填</span>
      </div>
      {recommendationMeta && (
        <Tooltip title={recommendationMeta.detail}>
          <Tag
            className="execution-workflow-card__recommendation"
            color={recommendationMeta.color}
            icon={recommendationMeta.icon}
          >
            {recommendationMeta.text}
          </Tag>
        </Tooltip>
      )}
      {candidatePreviewName && (
        <Tooltip
          title={candidatePreview?.relative_path || candidatePreviewName}
          placement="topLeft"
        >
          <div className="execution-workflow-card__file-preview">
            <FileSearchOutlined />
            <span>示例文件：{candidatePreviewName}</span>
          </div>
        </Tooltip>
      )}
      <div className="execution-workflow-card__footer">
        {!available ? (
          <Tooltip title={availability.message}>
            <Text type="danger">
              <StopOutlined /> {availability.message}
            </Text>
          </Tooltip>
        ) : lastStatus ? (
          <Tag color={lastStatus.color}>{lastStatus.text}</Tag>
        ) : (
          <Text type="secondary">尚无运行记录</Text>
        )}
        <Text type="secondary">
          <ClockCircleOutlined /> {workflow.updated_at
            ? dayjs(workflow.updated_at).format('MM-DD HH:mm')
            : '暂无更新'}
        </Text>
      </div>
    </Card>
  );
}

export default function ExecutionCatalog() {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const { canRunWorkflow } = useExecutionAuth();
  const [inspectionNumber, setInspectionNumber] = useState(
    searchParams.get('number') || '',
  );
  const [selectedCategories, setSelectedCategories] = useState(
    searchParams.getAll('category'),
  );
  const [categories, setCategories] = useState([]);
  const [workflows, setWorkflows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [recommendationSnapshot, setRecommendationSnapshot] = useState(null);
  const [recommendationLoading, setRecommendationLoading] = useState(false);
  const [recommendationFailed, setRecommendationFailed] = useState(false);
  const [pendingScrollWorkflowId, setPendingScrollWorkflowId] = useState(null);
  const recommendationTimerRef = useRef(null);
  const recommendationAbortRef = useRef(null);
  const recommendationRequestIdRef = useRef(0);

  const loadCatalog = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [categoryRows, workflowRows] = await Promise.all([
        getExecutionCategories(),
        getExecutionWorkflows({ published: true }),
      ]);
      setCategories(categoryRows);
      setWorkflows(workflowRows);
    } catch (requestError) {
      setError(requestError);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadCatalog();
  }, [loadCatalog]);

  useEffect(() => {
    const next = new URLSearchParams();
    if (inspectionNumber.trim()) {
      next.set('number', inspectionNumber.trim());
    }
    selectedCategories.forEach(category => next.append('category', category));
    setSearchParams(next, { replace: true });
  }, [inspectionNumber, selectedCategories, setSearchParams]);

  const normalizedRecommendationQuery = useMemo(() => ({
    inspectionNumber: inspectionNumber.trim(),
    preferredCategories: [...selectedCategories].sort(),
  }), [inspectionNumber, selectedCategories]);

  const recommendationQueryKey = useMemo(
    () => JSON.stringify(normalizedRecommendationQuery),
    [normalizedRecommendationQuery],
  );

  const requestRecommendations = useCallback(async (
    query,
    { scrollToFirst = false } = {},
  ) => {
    recommendationAbortRef.current?.abort();
    const controller = new AbortController();
    recommendationAbortRef.current = controller;
    const requestId = recommendationRequestIdRef.current + 1;
    recommendationRequestIdRef.current = requestId;
    const queryKey = JSON.stringify(query);
    setRecommendationLoading(true);
    setRecommendationFailed(false);
    try {
      const rows = await getExecutionCatalogRecommendations(query, {
        signal: controller.signal,
      });
      if (
        controller.signal.aborted
        || recommendationRequestIdRef.current !== requestId
      ) {
        return;
      }
      const rankedRows = rows.map((row, index) => ({
        ...row,
        rank: Number.isFinite(Number(row.rank)) ? Number(row.rank) : index,
      }));
      setRecommendationSnapshot({ queryKey, rows: rankedRows });
      if (scrollToFirst) {
        setPendingScrollWorkflowId(rankedRows[0]?.workflow_id || null);
      }
    } catch (requestError) {
      if (
        controller.signal.aborted
        || recommendationRequestIdRef.current !== requestId
      ) {
        return;
      }
      setRecommendationSnapshot(null);
      setRecommendationFailed(true);
    } finally {
      if (recommendationRequestIdRef.current === requestId) {
        setRecommendationLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    if (recommendationTimerRef.current) {
      clearTimeout(recommendationTimerRef.current);
    }
    recommendationAbortRef.current?.abort();
    recommendationRequestIdRef.current += 1;
    setPendingScrollWorkflowId(null);

    const hasRecommendationCriteria = Boolean(
      normalizedRecommendationQuery.inspectionNumber
      || normalizedRecommendationQuery.preferredCategories.length,
    );
    if (!hasRecommendationCriteria) {
      setRecommendationSnapshot(null);
      setRecommendationLoading(false);
      setRecommendationFailed(false);
      return undefined;
    }

    setRecommendationLoading(true);
    setRecommendationFailed(false);
    recommendationTimerRef.current = setTimeout(() => {
      requestRecommendations(normalizedRecommendationQuery);
    }, 350);

    return () => {
      if (recommendationTimerRef.current) {
        clearTimeout(recommendationTimerRef.current);
      }
      recommendationAbortRef.current?.abort();
      recommendationRequestIdRef.current += 1;
    };
  }, [normalizedRecommendationQuery, requestRecommendations]);

  const runRecommendationImmediately = useCallback(() => {
    if (recommendationTimerRef.current) {
      clearTimeout(recommendationTimerRef.current);
    }
    const hasRecommendationCriteria = Boolean(
      normalizedRecommendationQuery.inspectionNumber
      || normalizedRecommendationQuery.preferredCategories.length,
    );
    if (!hasRecommendationCriteria) {
      setRecommendationSnapshot(null);
      setRecommendationLoading(false);
      setRecommendationFailed(false);
      return;
    }
    requestRecommendations(normalizedRecommendationQuery, {
      scrollToFirst: true,
    });
  }, [normalizedRecommendationQuery, requestRecommendations]);

  const categoryOptions = useMemo(() => {
    const fromApi = categories.map(category => ({
      value: categoryKeyOf(category),
      label: category.name,
    }));
    if (fromApi.length) {
      return fromApi;
    }
    return [...new Map(workflows.map((workflow) => {
      const category = getCategory(workflow);
      const categoryKey = categoryKeyOf(category);
      return [categoryKey, { value: categoryKey, label: category.name }];
    })).values()];
  }, [categories, workflows]);

  const activeRecommendations = useMemo(() => {
    if (recommendationSnapshot?.queryKey !== recommendationQueryKey) {
      return null;
    }
    return recommendationSnapshot.rows;
  }, [recommendationQueryKey, recommendationSnapshot]);

  const recommendationByWorkflow = useMemo(
    () => new Map(
      (activeRecommendations || []).map(row => [String(row.workflow_id), row]),
    ),
    [activeRecommendations],
  );

  const groupedWorkflows = useMemo(() => {
    const groups = new Map();
    const defaultWorkflowOrder = new Map(
      workflows.map((workflow, index) => [String(workflowIdOf(workflow)), index]),
    );
    const defaultCategoryOrder = new Map(
      categories.map((category, index) => [categoryKeyOf(category), index]),
    );
    const recommendationRank = new Map(
      (activeRecommendations || []).map((row, index) => [
        String(row.workflow_id),
        Number.isFinite(Number(row.rank)) ? Number(row.rank) : index,
      ]),
    );
    workflows.forEach((workflow) => {
      const category = getCategory(workflow);
      const id = categoryKeyOf(category);
      if (!groups.has(id)) {
        groups.set(id, {
          category: { ...category, key: id },
          defaultOrder: defaultCategoryOrder.get(id) ?? groups.size,
          workflows: [],
        });
      }
      groups.get(id).workflows.push(workflow);
    });
    const orderedGroups = [...groups.values()];
    if (!activeRecommendations) {
      return orderedGroups;
    }
    const rankOf = workflow => recommendationRank.get(String(workflowIdOf(workflow)))
      ?? Number.POSITIVE_INFINITY;
    orderedGroups.forEach((group) => {
      group.workflows.sort((left, right) => (
        rankOf(left) - rankOf(right)
        || (defaultWorkflowOrder.get(String(workflowIdOf(left))) ?? 0)
          - (defaultWorkflowOrder.get(String(workflowIdOf(right))) ?? 0)
        || String(workflowIdOf(left)).localeCompare(String(workflowIdOf(right)))
      ));
      group.bestRecommendationRank = group.workflows.reduce(
        (best, workflow) => Math.min(best, rankOf(workflow)),
        Number.POSITIVE_INFINITY,
      );
    });
    return orderedGroups.sort((left, right) => (
      left.bestRecommendationRank - right.bestRecommendationRank
      || left.defaultOrder - right.defaultOrder
      || categoryKeyOf(left.category).localeCompare(categoryKeyOf(right.category))
    ));
  }, [activeRecommendations, categories, workflows]);

  useEffect(() => {
    if (!pendingScrollWorkflowId) {
      return;
    }
    const card = document.getElementById(
      `execution-workflow-${pendingScrollWorkflowId}`,
    );
    if (!card) {
      return;
    }
    card.scrollIntoView?.({ behavior: 'smooth', block: 'center' });
    setPendingScrollWorkflowId(null);
  }, [groupedWorkflows, pendingScrollWorkflowId]);

  const openRunForm = (workflow) => {
    if (!availabilityOf(workflow).available || !canRunWorkflow) {
      return;
    }
    const params = new URLSearchParams();
    if (inspectionNumber.trim()) {
      params.set('number', inspectionNumber.trim());
    }
    const query = params.toString();
    navigate(
      `/execution/workflows/${encodeURIComponent(workflowIdOf(workflow))}/start`
      + (query ? `?${query}` : ''),
    );
  };

  return (
    <div className="execution-page execution-catalog">
      <ExecutionChrome
        title="执行系统"
        subtitle="选择流程，让重复的检测工作按标准步骤可靠执行"
        actions={(
          <Button icon={<ReloadOutlined />} onClick={loadCatalog}>刷新</Button>
        )}
      />

      <section className="execution-catalog__search">
        <div className="execution-catalog__search-copy">
          <Text className="execution-eyebrow">开始一项检测工作</Text>
          <Title level={3}>查找并选择适用流程</Title>
          <Paragraph>
            检验编号可选；未填写也能先进入流程，在执行前补充。优先类别只影响推荐顺序。
          </Paragraph>
        </div>
        <div className="execution-catalog__filters">
          <Input
            size="large"
            value={inspectionNumber}
            onChange={event => setInspectionNumber(event.target.value)}
            onPressEnter={runRecommendationImmediately}
            prefix={<SearchOutlined />}
            placeholder="输入检验编号，例如 26X910095-1"
            allowClear
            aria-label="检验编号"
          />
          <Select
            size="large"
            mode="multiple"
            maxTagCount="responsive"
            value={selectedCategories}
            onChange={setSelectedCategories}
            options={categoryOptions}
            placeholder="选择优先类别（可多选）"
            allowClear
            aria-label="优先类别"
          />
          <div className="execution-catalog__recommendation-hint" aria-live="polite">
            {recommendationFailed ? (
              <Text type="warning">推荐识别暂不可用，已保持默认顺序，您仍可手动选择流程。</Text>
            ) : recommendationLoading ? (
              <Text type="secondary"><LoadingOutlined spin /> 正在识别适用流程…</Text>
            ) : activeRecommendations ? (
              <Text type="secondary">已按匹配程度排序，悬停卡片状态可查看匹配详情。</Text>
            ) : (
              <Text type="secondary">可直接选择流程；输入编号后会进一步识别候选文件并优化排序。</Text>
            )}
          </div>
        </div>
      </section>

      {error && (
        <Alert
          showIcon
          type="error"
          message="流程目录加载失败"
          description={error.message}
          action={<Button onClick={loadCatalog}>重试</Button>}
        />
      )}

      {loading ? (
        <div className="execution-catalog__skeleton">
          {[0, 1, 2, 3].map(item => <Card key={item}><Skeleton active /></Card>)}
        </div>
      ) : groupedWorkflows.length ? (
        <div className="execution-workflow-groups">
          {groupedWorkflows.map(group => (
            <section className="execution-workflow-group" key={categoryKeyOf(group.category)}>
              <div className="execution-workflow-group__header">
                <div>
                  <h2>{group.category.name}</h2>
                  {group.category.description && <p>{group.category.description}</p>}
                </div>
                <Tag>{group.workflows.length} 个流程</Tag>
              </div>
              <div className="execution-workflow-grid">
                {group.workflows.map(workflow => (
                  <WorkflowCard
                    key={workflowIdOf(workflow)}
                    workflow={workflow}
                    canRun={canRunWorkflow}
                    onRun={openRunForm}
                    recommendation={recommendationByWorkflow.get(
                      String(workflowIdOf(workflow)),
                    )}
                    recommendationLoading={
                      recommendationLoading
                      && Boolean(
                        normalizedRecommendationQuery.inspectionNumber
                        || normalizedRecommendationQuery.preferredCategories.length
                      )
                    }
                  />
                ))}
              </div>
            </section>
          ))}
        </div>
      ) : (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="当前没有可显示的已发布流程"
        >
          <Button onClick={loadCatalog}>刷新流程目录</Button>
        </Empty>
      )}

    </div>
  );
}
