export type Day = "lunes" | "martes" | "miercoles" | "jueves" | "viernes";

export type TimeSlot = {
  day: Day;
  start: string;
  end: string;
};

export type Participant = {
  id: string;
  name: string;
  availability: TimeSlot[];
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
  updated_at: string;
};

export type DecisionRecord = {
  id: string;
  option: TimeOption;
  summary: string;
  confirmed_by: string;
  source: "panel" | "whatsapp" | "api";
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
};

export type TokenUsage = {
  provider: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number;
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
  group_jid: string | null;
  group_name: string | null;
  group_participant_count: number | null;
};

export type ChannelMessage = {
  id: string;
  sender: string;
  text: string;
  kind: "human" | "agent";
  detected_invocation: boolean;
  created_at: string;
};

export type Session = {
  id: string;
  schema_version?: number;
  title: string;
  participants: Participant[];
  options: TimeOption[];
  availability_matrix: AvailabilityCell[];
  missing_info: string[];
  insights: string[];
  messages: ChatMessage[];
  channel_config: ChannelConfig;
  channel_messages: ChannelMessage[];
  last_processing: ProcessingSummary | null;
  last_agent_reply: string | null;
  selected_option: TimeOption | null;
  decision_summary: string | null;
  decision_history: DecisionRecord[];
  archived_at: string | null;
  status: "draft" | "calculated" | "confirmed";
};

export type RuntimeInfo = {
  provider: string;
  provider_label: string;
  model: string;
  cache_enabled: boolean;
  fallback_enabled: boolean;
  gemini_configured: boolean;
  warnings: string[];
};
