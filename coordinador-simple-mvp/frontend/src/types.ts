export type Day = "lunes" | "martes" | "miercoles" | "jueves" | "viernes";

export type TimeSlot = {
  day: Day;
  start: string;
  end: string;
  week_offset?: number;
};

export type Participant = {
  id: string;
  name: string;
  external_id?: string | null;
  availability: TimeSlot[];
  required?: boolean;
  priority?: number;
};

export type TimeOption = {
  id: string;
  day: Day;
  start: string;
  end: string;
  available_participants: string[];
  unavailable_participants: string[];
  score: number;
  coverage_percent: number;
  explanation: string;
  weighted_score?: number;
  required_met?: boolean;
  required_missing?: string[];
};

export type ProcessingSummary = {
  source: string;
  confidence: "high" | "medium" | "low";
  confidence_label: string;
  detail: string;
  participants_detected: number;
  removals_detected: number;
  cached: boolean;
  fallback_used: boolean;
  quality_flags: string[];
  habitual_used?: boolean;
  retrieval_used?: boolean;
  retrieval_source?: string | null;
  retrieval_participant_count?: number;
  retrieval_past_decision_count?: number;
  retrieval_preview?: string;
  updated_at: string;
};

export type DecisionRecord = {
  id: string;
  option: TimeOption;
  summary: string;
  confirmed_by: string;
  source: "panel" | "whatsapp" | "api";
  event_date: string | null;
  calendar_event: CalendarEventSnapshot | null;
  external_id: string | null;
  created_at: string;
};

export type AvailabilityCell = {
  day: Day;
  start: string;
  end: string;
  available_participants: string[];
  unavailable_participants: string[];
  score: number;
  coverage_percent: number;
  weighted_score?: number;
  required_met?: boolean;
  required_missing?: string[];
};

export type TokenUsage = {
  provider: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number;
  time_to_first_token_ms: number | null;
  cached: boolean;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  source: string | null;
  token_usage: TokenUsage | null;
  created_at: string;
};

export type ReplyFormat = "text" | "image" | "both";

export type ChannelConfig = {
  listening_enabled: boolean;
  trigger_word: string;
  reply_format: ReplyFormat;
  workday_start_hour: number;
  workday_end_hour: number;
  group_jid: string | null;
  group_name: string | null;
  group_participant_count: number | null;
  group_participant_ids: string[];
  coordinator_ids: string[];
  owner_admin: string | null;
};

export type ChannelMessage = {
  id: string;
  sender: string;
  sender_id?: string | null;
  text: string;
  kind: "human" | "agent";
  detected_invocation: boolean;
  external_id?: string | null;
  reply_to_external_id?: string | null;
  reply_format?: ReplyFormat | null;
  attachment_kind?: "calendar" | null;
  created_at: string;
};

export type ChannelCommandReceipt = {
  external_id: string;
  command_name: string;
  reply: string;
  reply_format: ReplyFormat;
  attachment_kind: "calendar" | null;
  document_text: string | null;
  created_at: string;
};

export type CalendarEventSnapshot = {
  uid: string;
  start_at: string;
  end_at: string;
  timezone: string;
  dtstamp: string;
  google_event_id?: string | null;
  google_event_html_link?: string | null;
};

export type GoogleCalendarStatus = {
  configured: boolean;
  connected: boolean;
  account_email: string | null;
  connected_at: string | null;
};

export type Session = {
  id: string;
  schema_version?: number;
  title: string;
  owner_username: string | null;
  link_code: string | null;
  linked_at: string | null;
  assigned_usernames: string[];
  chief_participant_id: string | null;
  participants: Participant[];
  options: TimeOption[];
  availability_matrix: AvailabilityCell[];
  missing_info: string[];
  insights: string[];
  messages: ChatMessage[];
  channel_config: ChannelConfig;
  channel_messages: ChannelMessage[];
  channel_context_message_id?: string | null;
  channel_command_receipts: ChannelCommandReceipt[];
  last_processing: ProcessingSummary | null;
  last_agent_reply: string | null;
  proposal_revision: number;
  selected_option: TimeOption | null;
  selected_event_date: string | null;
  selected_calendar_event: CalendarEventSnapshot | null;
  decision_summary: string | null;
  decision_history: DecisionRecord[];
  archived_at: string | null;
  status: "draft" | "calculated" | "confirmed";
  habitual_slot?: TimeSlot | null;
  habitual_history_index?: number | null;
};

export type AppUser = {
  username: string;
  display_name: string;
  is_admin: boolean;
  role: "platform_admin" | "group_admin";
};

export type RuntimeInfo = {
  provider: string;
  provider_label: string;
  model: string;
  cache_enabled: boolean;
  fallback_enabled: boolean;
  gemini_configured: boolean;
  gemini_key_count: number;
  gemini_available_key_count: number;
  gemini_active_key_name: string | null;
  gemini_key_management_enabled: boolean;
  warnings: string[];
  actor: string;
  is_superadmin: boolean;
};

export type LlmKeyStatus = "unverified" | "ready" | "cooldown" | "invalid" | "incompatible" | "disabled";

export type LlmKey = {
  id: string;
  name: string;
  priority: number;
  enabled: boolean;
  status: LlmKeyStatus;
  source: "panel" | "legacy_env";
  managed: boolean;
  success_count: number;
  failure_count: number;
  request_count: number;
  error_count: number;
  quota_exhaustion_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number;
  ttft_sample_count: number;
  last_ttft_ms: number | null;
  average_ttft_ms: number | null;
  best_ttft_ms: number | null;
  worst_ttft_ms: number | null;
  last_generation_at: string | null;
  last_used_at: string | null;
  cooldown_until: string | null;
  last_error_code: number | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
};

export type LlmKeyAudit = {
  id: string;
  credential_id: string | null;
  credential_name: string;
  action: string;
  actor: string;
  result: string;
  created_at: string;
};

export type LlmKeyListResponse = {
  keys: LlmKey[];
  audit: LlmKeyAudit[];
  active_key_id: string | null;
  available_count: number;
  management_enabled: boolean;
};

export type GatewayStatus = {
  ok: boolean;
  connected: boolean;
  state: "starting" | "connecting" | "connected" | "reconnecting" | "logged_out" | "unavailable" | "unknown" | string;
  known_groups?: number;
  reconnect_attempt?: number;
  pending_messages?: number;
  dead_letter_messages?: number;
  queue_overflow_count?: number;
  last_queue_error_at?: string | null;
  last_connected_at?: string | null;
  last_disconnected_at?: string | null;
  checked_at: string;
};

export type OpsStatus = {
  ok: boolean;
  gateway: GatewayStatus;
};
