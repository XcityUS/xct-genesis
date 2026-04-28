import type { PilotBranch, PilotDataset, PilotStatus } from './pilot-data'
import latestSnapshot from './autoresearch-latest-snapshot.json'

interface RunSummary {
  run_id: string
  scene_id: string
  start_time: string
  tick_count: number
  agent_count: number
  dm_calls?: number
}

interface ApiState {
  run_id: string
  tick: number
  entities: RawEntity[]
  characters?: Record<string, unknown>
}

type RawEntity = Record<string, unknown> & {
  id: string
  type: string
}

interface Hypothesis {
  id: string
  claim: string
  rationale?: string
  status?: string
  published_as_paper?: string | null
  created_tick?: number
}

interface AgentEntity extends RawEntity {
  type: 'agent'
  hypotheses?: Hypothesis[]
  last_action?: { action?: string; tick?: number; params?: Record<string, unknown> }
}

interface ExperimentEntity extends RawEntity {
  type: 'experiment'
  author?: string
  commit?: string
  branch?: string
  val_loss?: number
  wall_time?: number
  status?: string
  hypothesis_id?: string | null
  description?: string
  submitted_tick?: number
  published?: boolean
}

interface PaperEntity extends RawEntity {
  type: 'paper'
  title?: string
  author?: string
  claim?: string
  abstract?: string
  method_commit?: string
  evidence_experiments?: string[]
  cites?: string[]
  hypothesis?: Hypothesis
  status?: string
  verified?: boolean
  verify_val_loss?: number
  verify_delta?: number
  expected_val_loss?: number
  reviews?: Array<{ reviewer?: string; verdict?: string; reasoning?: string }>
  created_tick?: number
}

interface CorpusEntity extends RawEntity {
  type: 'corpus'
  papers_accepted?: number
  papers_rejected?: number
  papers_contested?: number
  experiments_total?: number
  experiments_crashed?: number
  best_val_loss?: number
}

interface HypothesisItem {
  agent: AgentEntity
  hypothesis: Hypothesis
}

export interface AutoresearchPilotDatasetResult {
  dataset: PilotDataset
  source: 'api' | 'snapshot'
}

export async function fetchLatestAutoresearchPilotDataset(signal?: AbortSignal): Promise<AutoresearchPilotDatasetResult | null> {
  let runs: RunSummary[]
  try {
    runs = await fetchJson<RunSummary[]>('/api/runs', signal)
  } catch {
    return snapshotResult()
  }

  const candidates = runs
    .filter(run => run.scene_id === 'autoresearch')
    .sort((a, b) => (b.start_time || '').localeCompare(a.start_time || ''))

  for (const run of candidates) {
    try {
      const state = await fetchJson<ApiState>(`/api/runs/${run.run_id}/state`, signal)
      const dataset = buildPilotDataset(run, state)
      if (dataset) return { dataset, source: 'api' }
    } catch {
      // Some recent runs can be partial; keep walking back until a usable run appears.
    }
  }

  return snapshotResult()
}

async function fetchJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal })
  if (!response.ok) throw new Error(`${url} ${response.status}`)
  return response.json() as Promise<T>
}

function snapshotResult(): AutoresearchPilotDatasetResult | null {
  const snapshot = latestSnapshot as { run: RunSummary; state: ApiState }
  const dataset = buildPilotDataset(snapshot.run, snapshot.state)
  if (!dataset) return null
  return {
    dataset,
    source: 'snapshot',
  }
}

function buildPilotDataset(run: RunSummary, state: ApiState): PilotDataset | null {
  const entities = state.entities || []
  const agents = entities.filter(isAgent)
  const experiments = entities.filter(isExperiment)
  const papers = entities.filter(isPaper)
  const corpus = entities.find(isCorpus)

  if (!agents.length || (!experiments.length && !papers.length)) return null

  const baseline = experiments.find(exp => exp.status === 'ok' && !exp.hypothesis_id && isNumber(exp.val_loss))
  const baselineLoss = baseline?.val_loss
  const okExperiments = experiments.filter(exp => exp.status === 'ok' && isNumber(exp.val_loss))
  const bestExperiment = [...okExperiments].sort((a, b) => (a.val_loss || Infinity) - (b.val_loss || Infinity))[0]
  const acceptedPapers = papers.filter(paper => paper.status === 'accepted')
  const acceptedBestPaper = [...acceptedPapers].sort((a, b) => paperLoss(a) - paperLoss(b))[0]
  const leadPaper = acceptedBestPaper || papers.find(paper => paper.status === 'contested') || papers[0]
  const hypotheses = flattenHypotheses(agents)
  const branches = buildTopologyBranches(hypotheses, experiments, papers, baselineLoss)

  if (!branches.length) return null

  const bestLossText = bestExperiment?.val_loss ? `best val_loss ${fmt(bestExperiment.val_loss)}` : 'best val_loss n/a'
  const leadTitle = leadPaper?.title || bestExperiment?.description || 'latest autoresearch run'
  const researchTopic = '如何降低小型 GPT 的 validation loss'
  const winnerLabel = acceptedBestPaper ? '优胜 paper' : leadPaper ? '关键 paper' : '领先结果'
  const winnerId = acceptedBestPaper?.id || leadPaper?.id || bestExperiment?.id || 'latest result'
  const winnerMethod = acceptedBestPaper ? paperMethodSummary(acceptedBestPaper) : shortTitle(bestExperiment?.description || leadTitle)
  const winnerEffect = acceptedBestPaper ? paperLiftSummary(acceptedBestPaper, baselineLoss) : experimentLiftSummary(bestExperiment, baselineLoss)
  const accepted = corpus?.papers_accepted ?? acceptedPapers.length
  const contested = corpus?.papers_contested ?? papers.filter(paper => paper.status === 'contested').length
  const crashed = corpus?.experiments_crashed ?? experiments.filter(exp => exp.status === 'crashed').length
  const verdictBullets = acceptedBestPaper
    ? paperInsightBullets(acceptedBestPaper, hypotheses.length, experiments.length, papers.length)
    : fallbackInsightBullets(hypotheses.length, experiments.length, papers.length, bestLossText, baselineLoss)

  return {
    eyebrow: `RUN · ${formatDate(run.start_time)}`,
    title: 'Auto研究结果总汇',
    subtitle: researchTopic,
    meta: `run ${run.run_id} · ${state.tick || run.tick_count} ticks · ${experiments.length} experiments`,
    verdict: {
      lead: `<strong>${winnerLabel}：${escapeHtml(winnerId)}。</strong>${escapeHtml(winnerMethod)}，${escapeHtml(winnerEffect)}。`,
      bullets: verdictBullets,
      recommend: acceptedBestPaper
        ? `先看 ${acceptedBestPaper.id} 的完整结论:${acceptedBestPaper.title || acceptedBestPaper.claim || acceptedBestPaper.id}。`
        : '该 run 还没有 accepted paper,先看 best experiment 和 contested/refuted hypothesis。',
      deliverables: [
        { icon: 'R', name: `run_${run.run_id}`, meta: `${state.tick || run.tick_count} ticks\n${agents.length} agents` },
        { icon: 'P', name: `${papers.length} papers`, meta: `${accepted} accepted\n${contested} contested` },
        { icon: 'E', name: `${experiments.length} experiments`, meta: `${okExperiments.length} ok\n${crashed} crashed` },
        { icon: 'B', name: bestExperiment?.id || 'best experiment', meta: `${bestLossText}\n${shortCommit(bestExperiment?.commit)}` },
      ],
      actions: ['刷新 latest run', '打开 run replay', '导出 PDF'],
    },
    question: {
      Topic: researchTopic,
      Scope: `对象:autoresearch · 数据:${experiments.length} experiments / ${papers.length} papers · 资源:${agents.length} agents · 门槛:${baselineLoss ? `baseline ${fmt(baselineLoss)}` : 'saved run state'}`,
      'Why now': `最新有效 autoresearch run 是 ${run.run_id},开始于 ${formatDateTime(run.start_time)}。`,
    },
    panel: agents.map(agent => ({
      avatar: agent.id.slice(0, 1).toUpperCase(),
      name: agent.id,
      bio: `${agent.hypotheses?.length || 0} hypotheses${agent.last_action?.action ? ` · last ${agent.last_action.action}@t${agent.last_action.tick || '?'}` : ''}`,
    })),
    rules: [
      '<strong>latest run 选择</strong>: scene_id=autoresearch 且有 state/experiment 的最新 run',
      '<strong>拓扑层级</strong>: graph 只显示 paper / verified outcome / 未发表 best experiment',
      '<strong>详情层级</strong>: 原始 hypothesis 保留为计数和 evidence,不直接铺成 56 条并行分支',
      '<strong>状态映射</strong>: accepted → chosen · contested/refuted/crashed → killed · 其余 → parked',
    ],
    branchMap: { shape: papers.length > 1 ? 'tree' : 'wide' },
    branches,
    confidence: {
      tested: [
        `state.json loaded · ${entities.length} entities`,
        `${experiments.length} experiments · ${papers.length} papers`,
        `${agents.length} agents · ${hypotheses.length} hypotheses`,
      ],
      untested: [
        'run summary 仍是启发式转换,不是 LLM-written final memo',
        '跨 run 比较还没有接入',
        `${hypotheses.length} 条原始 hypothesis 未在拓扑第一层展开`,
      ],
      next: [
        '加后端 /api/runs/latest?scene=autoresearch 过滤空 run',
        '加 /api/runs/{id}/pilot 固化转换逻辑',
        '把 results.tsv 暴露成 endpoint,用于更精确的实验表',
      ],
    },
  }
}

function flattenHypotheses(agents: AgentEntity[]): HypothesisItem[] {
  return agents.flatMap(agent => (agent.hypotheses || []).map(hypothesis => ({ agent, hypothesis })))
}

function buildTopologyBranches(
  hypotheses: HypothesisItem[],
  experiments: ExperimentEntity[],
  papers: PaperEntity[],
  baselineLoss?: number,
): PilotBranch[] {
  if (!papers.length) {
    return buildAgentTrackBranches(hypotheses, experiments, baselineLoss)
  }

  const sortedPapers = [...papers].sort((a, b) => (a.created_tick || 0) - (b.created_tick || 0) || a.id.localeCompare(b.id))
  const paperLetters = new Map<string, string>()
  sortedPapers.forEach((paper, index) => paperLetters.set(paper.id, indexLabel(index)))

  const paperBranches = sortedPapers.map((paper, index) => buildPaperBranch(paper, index, experiments, paperLetters, baselineLoss))
  const paperEvidenceIds = new Set(sortedPapers.flatMap(paper => paper.evidence_experiments || []))
  const bestPublishedLoss = sortedPapers.reduce((best, paper) => Math.min(best, paperLoss(paper)), Infinity)
  const notableExperiments = experiments
    .filter(exp => !paperEvidenceIds.has(exp.id))
    .filter(exp => exp.status === 'ok' && isNumber(exp.val_loss) && exp.hypothesis_id)
    .filter(exp => !Number.isFinite(bestPublishedLoss) || (exp.val_loss || Infinity) < bestPublishedLoss - 0.005)
    .sort((a, b) => (a.val_loss || Infinity) - (b.val_loss || Infinity))
    .slice(0, 3)
    .map((exp, offset) => buildExperimentBranch(exp, paperBranches.length + offset, sortedPapers, paperLetters, baselineLoss))

  return [...paperBranches, ...notableExperiments]
}

function buildPaperBranch(
  paper: PaperEntity,
  index: number,
  experiments: ExperimentEntity[],
  paperLetters: Map<string, string>,
  baselineLoss?: number,
): PilotBranch {
  const evidenceExperiments = experiments.filter(exp => (paper.evidence_experiments || []).includes(exp.id))
  const best = [...evidenceExperiments].filter(exp => isNumber(exp.val_loss)).sort((a, b) => (a.val_loss || Infinity) - (b.val_loss || Infinity))[0]
  const createdTick = paper.created_tick ?? minTick(evidenceExperiments) ?? index
  const primaryCitation = (paper.cites || []).find(id => paperLetters.has(id))
  const relations = (paper.cites || [])
    .map(id => paperLetters.get(id))
    .filter((target): target is string => Boolean(target))
    .map(target => ({ target, kind: 'cites' as const, confidence: 'field' as const }))
  const citeText = paper.cites?.length ? ` · cites ${paper.cites.join(' + ')}` : ''
  const reviewText = paper.reviews?.length ? `${paper.reviews.filter(review => review.verdict === 'accept').length}/${paper.reviews.length} accept` : 'no reviews'
  const lossText = best?.val_loss ? `val_loss ${fmt(best.val_loss)}` : paperLoss(paper) < Infinity ? `val_loss ${fmt(paperLoss(paper))}` : 'no metric'
  const deltaText = best?.val_loss && baselineLoss ? ` · Δ ${signed(best.val_loss - baselineLoss)}` : ''

  return {
    letter: indexLabel(index),
    title: `Paper ${paper.id.replace('paper_', '')} · ${shortTitle(paper.title || paper.claim || paper.id)}`,
    kind: 'paper',
    status: paper.status === 'accepted' ? 'chosen' : paper.status === 'contested' || paper.status === 'rejected' ? 'killed' : 'parked',
    statusLabel: paper.status === 'accepted' ? '✓ Accepted' : paper.status === 'contested' ? '✗ Contested' : paper.status === 'rejected' ? '✗ Rejected' : '△ Review',
    createdTick,
    parent: primaryCitation ? paperLetters.get(primaryCitation) || null : null,
    relations,
    thesis: paper.claim || paper.abstract || paper.title,
    attempts: `${evidenceExperiments.length || (paper.evidence_experiments || []).length} evidence experiment · ${reviewText}${citeText}`,
    result: `${lossText}${deltaText}${paper.verified ? ' · verified' : ''}`,
    evidence: evidenceLines(evidenceExperiments, paper),
    decision: `${paper.id} · ${paper.status || 'paper'}${paper.verified ? ` · verify Δ ${fmt(paper.verify_delta || 0)}` : ''}`,
  }
}

function buildExperimentBranch(
  exp: ExperimentEntity,
  index: number,
  papers: PaperEntity[],
  paperLetters: Map<string, string>,
  baselineLoss?: number,
): PilotBranch {
  const parentPaperId = parentPaperFromText(exp.description || '', papers)
  const parent = parentPaperId ? paperLetters.get(parentPaperId) || null : null
  const lossText = isNumber(exp.val_loss) ? `val_loss ${fmt(exp.val_loss)}` : exp.status || 'experiment'
  const deltaText = isNumber(exp.val_loss) && baselineLoss ? ` · Δ ${signed(exp.val_loss - baselineLoss)}` : ''

  return {
    letter: indexLabel(index),
    title: `Experiment ${exp.id.replace('experiment_', '')} · ${shortTitle(exp.description || exp.id)}`,
    kind: 'pending_result',
    status: 'parked',
    statusLabel: '△ Unreviewed',
    createdTick: exp.submitted_tick ?? index,
    parent,
    relations: parent ? [{ target: parent, kind: 'builds_on', confidence: 'inferred' }] : [],
    thesis: firstSentence(exp.description || exp.id),
    attempts: `1 unpublished experiment · ${shortCommit(exp.commit)}`,
    result: `${lossText}${deltaText} · no paper yet`,
    evidence: [{ line: `${exp.id} · ${exp.status || 'unknown'}${isNumber(exp.val_loss) ? ` · val_loss ${fmt(exp.val_loss)}` : ''}` }],
    decision: 'best unpublished result · needs paper/review before becoming chosen',
  }
}

function buildAgentTrackBranches(hypotheses: HypothesisItem[], experiments: ExperimentEntity[], baselineLoss?: number): PilotBranch[] {
  const agents = [...new Map(hypotheses.map(item => [item.agent.id, item.agent])).values()]
  return agents.map((agent, index) => {
    const agentExperiments = experiments.filter(exp => exp.author === agent.id)
    const best = [...agentExperiments].filter(exp => exp.status === 'ok' && isNumber(exp.val_loss)).sort((a, b) => (a.val_loss || Infinity) - (b.val_loss || Infinity))[0]
    return {
      letter: indexLabel(index),
      title: `Track ${indexLabel(index)} · ${agent.id}`,
      kind: 'track',
      status: best ? 'parked' : 'killed',
      statusLabel: best ? '△ In progress' : '✗ No result',
      createdTick: best?.submitted_tick ?? index,
      thesis: `${agent.hypotheses?.length || 0} hypotheses explored by ${agent.id}`,
      attempts: `${agentExperiments.length} experiments`,
      result: best?.val_loss ? `best val_loss ${fmt(best.val_loss)}${baselineLoss ? ` · Δ ${signed(best.val_loss - baselineLoss)}` : ''}` : 'no successful experiment',
      evidence: evidenceLines(agentExperiments.slice(0, 3)),
      decision: 'no paper yet · aggregated agent track',
    }
  })
}

function buildBranch(
  item: { agent: AgentEntity; hypothesis: Hypothesis },
  index: number,
  experiments: ExperimentEntity[],
  papers: PaperEntity[],
  baselineLoss?: number,
): PilotBranch {
  const { agent, hypothesis } = item
  const relatedExperiments = experiments.filter(exp => exp.author === agent.id && exp.hypothesis_id === hypothesis.id)
  const relatedPapers = papers.filter(paper => paper.author === agent.id && paper.hypothesis?.id === hypothesis.id)
  const paper = relatedPapers[0]
  const okExperiments = relatedExperiments.filter(exp => exp.status === 'ok' && isNumber(exp.val_loss))
  const best = [...okExperiments].sort((a, b) => (a.val_loss || Infinity) - (b.val_loss || Infinity))[0]
  const status = branchStatus(hypothesis, relatedExperiments, paper, baselineLoss)
  const paperId = paper?.id ? ` · ${paper.id}` : ''
  const experimentCount = relatedExperiments.length
  const resultParts: string[] = []

  if (best?.val_loss) {
    resultParts.push(`best val_loss ${fmt(best.val_loss)}`)
    if (baselineLoss) resultParts.push(`Δ ${signed(best.val_loss - baselineLoss)}`)
  }
  if (paper?.status) resultParts.push(`paper ${paper.status}`)
  if (!resultParts.length && relatedExperiments.length) resultParts.push(`${relatedExperiments.length} experiments`)
  if (!resultParts.length) resultParts.push(hypothesis.status || 'proposed')

  return {
    letter: indexLabel(index),
    title: `Branch ${indexLabel(index)} · ${shortTitle(paper?.title || hypothesis.claim)}`,
    status,
    statusLabel: statusLabel(status, paper, hypothesis),
    thesis: hypothesis.claim,
    attempts: `${experimentCount} 次 experiment${paperId}`,
    result: resultParts.join(' · '),
    evidence: evidenceLines(relatedExperiments, paper),
    decision: branchDecision(hypothesis, paper),
  }
}

function branchStatus(hypothesis: Hypothesis, experiments: ExperimentEntity[], paper?: PaperEntity, baselineLoss?: number): PilotStatus {
  if (paper?.status === 'accepted') return 'chosen'
  if (paper?.status === 'contested' || paper?.status === 'rejected' || hypothesis.status === 'refuted') return 'killed'
  if (experiments.length && experiments.every(exp => exp.status === 'crashed')) return 'killed'
  const best = experiments
    .filter(exp => exp.status === 'ok' && isNumber(exp.val_loss))
    .sort((a, b) => (a.val_loss || Infinity) - (b.val_loss || Infinity))[0]
  if (best?.val_loss && baselineLoss && best.val_loss > baselineLoss + 0.02) return 'killed'
  return 'parked'
}

function statusLabel(status: PilotStatus, paper?: PaperEntity, hypothesis?: Hypothesis) {
  if (paper?.status === 'accepted') return '✓ Accepted'
  if (paper?.status === 'contested') return '✗ Contested'
  if (paper?.status === 'under_review') return '△ Review'
  if (hypothesis?.status === 'refuted') return '✗ Refuted'
  return status === 'chosen' ? '✓ Chosen' : status === 'killed' ? '✗ Killed' : '△ Parked'
}

function evidenceLines(experiments: ExperimentEntity[], paper?: PaperEntity): Array<{ line: string; qt?: boolean }> {
  const experimentLines: Array<{ line: string; qt?: boolean }> = experiments.slice(0, 3).map(exp => ({
    line: `${exp.id} · ${exp.status || 'unknown'}${isNumber(exp.val_loss) ? ` · val_loss ${fmt(exp.val_loss)}` : ''}`,
  }))
  const review = paper?.reviews?.[0]
  if (review?.reasoning) {
    experimentLines.push({
      qt: true,
      line: `${review.reviewer || 'reviewer'}: ${truncate(review.reasoning, 180)}`,
    })
  }
  return experimentLines
}

function branchDecision(hypothesis: Hypothesis, paper?: PaperEntity) {
  if (paper) {
    const verify = paper.verified ? `verified Δ ${fmt(paper.verify_delta || 0)}` : 'not verified'
    return `${paper.id} · ${paper.status || 'paper'} · ${verify}`
  }
  return `hypothesis ${hypothesis.status || 'proposed'}`
}

function paperMethodSummary(paper: PaperEntity) {
  const text = `${paper.title || ''} ${paper.claim || ''} ${paper.abstract || ''}`.toLowerCase()
  const methods: string[] = []

  if (/\brope\b|rotary position/.test(text)) methods.push('RoPE 位置编码')
  if (/swiglu|gated activation|gated mlp/.test(text)) methods.push('SwiGLU gated MLP')
  if (/rmsnorm/.test(text)) methods.push('RMSNorm')
  if (/weight decay|wd=|regularization/.test(text)) methods.push('weight decay 调整')
  if (/rewarmup|re-warmup/.test(text)) methods.push('LR rewarmup')
  else if (/warmup/.test(text)) methods.push('warmup 调度')
  if (/label smoothing/.test(text)) methods.push('label smoothing')
  if (/qk-layernorm|qk layernorm/.test(text)) methods.push('QK-LayerNorm')
  if (/parallel attention/.test(text)) methods.push('parallel attention')
  if (/lion optimizer/.test(text)) methods.push('Lion optimizer')
  if (/muon optimizer/.test(text)) methods.push('Muon optimizer')

  const unique = [...new Set(methods)]
  return unique.length ? unique.slice(0, 3).join(' + ') : shortTitle(paper.claim || paper.title || paper.id)
}

function paperInsightBullets(paper: PaperEntity, hypothesisCount: number, experimentCount: number, paperCount: number) {
  return [
    `胜出原因: ${shortMechanism(paper)}`,
    `对比含义: ${shortComparison(paper)}`,
    `证据基础: ${shortEvidence(paper, hypothesisCount, experimentCount, paperCount)}`,
  ]
}

function fallbackInsightBullets(hypothesisCount: number, experimentCount: number, paperCount: number, bestLossText: string, baselineLoss?: number) {
  return [
    `当前结论还没有 accepted paper,只能作为候选结果处理。`,
    `探索覆盖 ${hypothesisCount} 个 hypothesis、${experimentCount} 个 experiment、${paperCount} 篇 paper,但缺少可引用的最终结论。`,
    `现有最优实验是 ${bestLossText}${baselineLoss ? `,baseline ${fmt(baselineLoss)}` : ''},需要补 paper/review 才能进入主结论。`,
  ]
}

function shortMechanism(paper: PaperEntity) {
  const text = `${paper.title || ''} ${paper.claim || ''} ${paper.abstract || ''}`.toLowerCase()
  if (/\brope\b|rotary position/.test(text) && /swiglu|gated activation|gated mlp/.test(text)) {
    return 'attention 位置建模 + MLP gated 激活同时改。'
  }
  if (/\brope\b|rotary position/.test(text) && /weight decay|regularization/.test(text)) {
    return 'RoPE 后重新校准 weight decay。'
  }
  if (/\brope\b|rotary position/.test(text)) {
    return '用 RoPE 替代 learned positional embedding。'
  }
  if (/swiglu|gated activation|gated mlp/.test(text)) {
    return '用 SwiGLU 替代普通 MLP 激活。'
  }
  return truncate(firstSentence(paper.claim || paper.abstract || paper.title || paper.id), 42)
}

function shortComparison(paper: PaperEntity) {
  const text = `${paper.title || ''} ${paper.claim || ''} ${paper.abstract || ''}`.toLowerCase()
  if (/\brope\b|rotary position/.test(text) && /swiglu|gated activation|gated mlp/.test(text)) {
    return '不是单点调参,是结构组合收益。'
  }
  if (/weight decay|regularization/.test(text)) {
    return '架构变化后,旧正则强度不能照搬。'
  }
  return '同一评审口径下胜过其它候选。'
}

function shortEvidence(paper: PaperEntity, hypothesisCount: number, experimentCount: number, paperCount: number) {
  const reviewCount = paper.reviews?.length || 0
  const acceptCount = paper.reviews?.filter(review => review.verdict === 'accept').length || 0
  const verify = paper.verified ? 'verify 复跑通过' : 'verify 状态待补'
  const review = reviewCount ? `${acceptCount}/${reviewCount} reviewer accept` : 'review 信息不足'
  return `${verify}; ${review}; ${hypothesisCount}/${experimentCount}/${paperCount} 做对照。`
}

function paperLiftSummary(paper: PaperEntity, baselineLoss?: number) {
  const loss = paper.expected_val_loss || paper.verify_val_loss
  if (!isNumber(loss) || !isNumber(baselineLoss)) return '相对提升待确认'
  return `较 baseline 提升 ${percentImprovement(baselineLoss, loss)}`
}

function experimentLiftSummary(exp?: ExperimentEntity, baselineLoss?: number) {
  if (!isNumber(exp?.val_loss) || !isNumber(baselineLoss)) return '相对提升待确认'
  return `较 baseline 提升 ${percentImprovement(baselineLoss, exp.val_loss)}`
}

function percentImprovement(baseline: number, loss: number) {
  const improvement = ((baseline - loss) / baseline) * 100
  return `${improvement.toFixed(1)}%`
}

function paperLoss(paper: PaperEntity) {
  return paper.verify_val_loss || paper.expected_val_loss || Infinity
}

function isAgent(entity: RawEntity): entity is AgentEntity {
  return entity.type === 'agent'
}

function isExperiment(entity: RawEntity): entity is ExperimentEntity {
  return entity.type === 'experiment'
}

function isPaper(entity: RawEntity): entity is PaperEntity {
  return entity.type === 'paper'
}

function isCorpus(entity: RawEntity): entity is CorpusEntity {
  return entity.type === 'corpus'
}

function isNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function minTick(items: Array<{ submitted_tick?: number; created_tick?: number }>) {
  const ticks = items
    .map(item => item.submitted_tick ?? item.created_tick)
    .filter(isNumber)
  return ticks.length ? Math.min(...ticks) : undefined
}

function fmt(value: number) {
  return value.toFixed(Math.abs(value) < 0.01 ? 4 : 3)
}

function signed(value: number) {
  return `${value >= 0 ? '+' : ''}${fmt(value)}`
}

function shortCommit(commit?: string) {
  return commit ? commit.slice(0, 8) : 'no commit'
}

function shortTitle(text: string) {
  return truncate(text.replace(/\.$/, ''), 72)
}

function firstSentence(text: string) {
  const match = text.match(/^.*?[.!?。！？](?:\s|$)/)
  return truncate(match?.[0].trim() || text, 180)
}

function parentPaperFromText(text: string, papers: PaperEntity[]) {
  const explicit = text.match(/paper_\d+/)?.[0]
  if (explicit && papers.some(paper => paper.id === explicit)) return explicit
  const lower = text.toLowerCase()
  const mentioned = papers.find(paper => paper.title && lower.includes(paper.title.toLowerCase().slice(0, 24)))
  return mentioned?.id
}

function truncate(text: string, max: number) {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

function indexLabel(index: number) {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
  if (index < alphabet.length) return alphabet[index]
  return `H${index + 1}`
}

function formatDate(iso: string) {
  return iso ? iso.slice(0, 10) : 'latest'
}

function formatDateTime(iso: string) {
  return iso ? iso.replace('T', ' ').slice(0, 16) : 'unknown time'
}

function escapeHtml(text: string) {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}
