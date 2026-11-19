export interface Session {
  id: string;
  seq?: number;
  title: string;
  created_at: string;
}

export interface Run {
  id: string;
  seq: number;
  session_id: string;
  status: string;
  text?: string;
  kind?: string;
  result: { answer?: string; model_calls?: number; tool_calls?: number;
    diagnostic?: { code: string; transport_error: string | null; http_status: number | null; retryable: boolean; hint: string } } | null;
  error: string | null;
  created_at: string;
}

export interface Research {
  work_id: string;
  research_id: string | null;
  session_id: string;
  title: string;
  symbol: string;
  as_of: string | null;
  status: string;
  progress: string;
  content_status: string | null;
  report_available: boolean;
}

export interface Citation {
  id: string;
  claim: string | null;
  source: string;
  source_url: string | null;
  locator: string | null;
  excerpt?: string | null;
  note?: string;
}

export interface Report {
  research_id: string;
  origin_session_id: string;
  title: string;
  scope: { symbol: string; as_of: string; goal: string; [key: string]: unknown };
  execution_status: string;
  content_status: string;
  review_status: string;
  outcome_verified: boolean;
  key_findings: { claim_id: string; text: string; kind: string; citation_ids: string[] }[];
  sections: { id: string; title: string; markdown: string }[];
  citations: Citation[];
  limitations: string[];
  charts: unknown[];
}

export interface WebEvent {
  event_id: string;
  sequence: number;
  type: string;
  session_id: string;
  occurred_at: string;
  payload: { name?: string; message?: string; status?: string; research_id?: string; work_id?: string; run_id?: string };
}
