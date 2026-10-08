// Typed client for the Gradimize FastAPI backend.
//
// In dev, requests go to /api/* and Vite proxies them to :8000 (stripping
// /api). Override with VITE_API_BASE for production deployments.

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "/api";

// Shared access token (when the server has GRADIMIZE_TOKEN set). Stored in
// localStorage after the TokenGate prompt; attached as a Bearer header on
// fetches and as ?token= on EventSource/download URLs (which can't set
// headers).
const TOKEN_KEY = "gradimize.token";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setToken(token: string): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

function withToken(url: string): string {
  const token = getToken();
  if (!token) return url;
  return `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const res = await fetch(`${API_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      // non-JSON error body; keep statusText
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// URL for browser-driven downloads (CSV exports etc.)
export function fileUrl(path: string): string {
  return withToken(`${API_BASE}${path}`);
}

// ─────────────────────────────────────────────────────────────────
// Types (mirror src/api/schemas.py and route response models)
// ─────────────────────────────────────────────────────────────────

export interface SystemStats {
  total_students: number;
  total_courses: number;
  total_faculty: number;
  total_rooms: number;
  total_time_slots: number;
  total_demands: number;
  schedules_count: number;
}

export interface ScheduleSummary {
  id: number;
  name: string;
  semester: string;
  status: string;
  created_at: string;
  sections_count: number;
}

export interface MeetingDetail {
  meeting_sequence: number;
  time_slot_id: number;
  time_slot_name: string;
  room_id: string;
  room_name: string;
}

export interface SectionDetail {
  id: number;
  crn: string;
  course_code: string;
  section_number: string;
  capacity: number;
  time_slot_name: string | null;
  time_slot_id: number | null;
  room_id: string | null;
  room_name: string | null;
  faculty_name: string | null;
  faculty_id: string | null;
  enrollment_count: number;
  is_pinned: boolean;
  pin_reason: string | null;
  meetings: MeetingDetail[];
}

export interface ScheduleDetail {
  id: number;
  name: string;
  semester: string;
  status: string;
  created_at: string;
  solver_stats: Record<string, unknown>;
  sections: SectionDetail[];
}

export interface SectionPinResponse {
  section_id: number;
  crn: string;
  is_pinned: boolean;
  time_slot_name: string | null;
  room_id: string | null;
  faculty_name: string | null;
  pin_reason: string | null;
}

export interface ConflictItem {
  kind: "room" | "faculty" | "student";
  detail: string;
  sections: { id: number; crn: string; course_code: string; section_number: string }[];
  affected_students: number;
}

export interface CheckConflictsResponse {
  schedule_id: number;
  total: number;
  room: number;
  faculty: number;
  student: number;
  conflicts: ConflictItem[];
}

export interface SectionViabilityInfo {
  section_id: number;
  crn: string;
  course_code: string;
  section_number: string;
  enrolled: number;
  capacity: number;
  min_enrollment: number;
  reseatable: number;
  stranded: number;
  stranded_grad_critical: number;
  stranded_students: string[];
  alternatives: { crn: string; section_number: string; seats_free: number }[];
  next_offering: string | null;
  recommendation: string;
}

export interface ViabilityReport {
  schedule_id: number;
  semester: string;
  total_sections: number;
  below_min: SectionViabilityInfo[];
}

export interface StudentQualityInfo {
  student_id: string;
  days_on_campus: number;
  total_gap_minutes: number;
  longest_gap_minutes: number;
  max_marathon_minutes: number;
  marathons_over_3h: number;
  heavy_days: number;
  earliest_start: string | null;
  latest_end: string | null;
  badness: number;
  lunch_crunch_days: number;
  longest_workday_minutes: number;
  long_workdays: number;
}

export interface DepartmentPrimeTimeInfo {
  department: string;
  total_meetings: number;
  prime_meetings: number;
  prime_share: number;
}

export interface StudentQualityReport {
  schedule_id: number;
  semester: string;
  total_students: number;
  mean_days_on_campus: number;
  median_days_on_campus: number;
  mean_gap_minutes: number;
  students_5_day: number;
  students_big_gap: number;
  students_heavy_marathon: number;
  students_no_lunch: number;
  students_long_workday: number;
  worst_students: StudentQualityInfo[];
  department_prime_time: DepartmentPrimeTimeInfo[];
}

export interface FinalsScheduleSummary {
  id: number;
  schedule_id: number;
  name: string;
  created_at: string;
  status: string;
  num_assignments: number;
}

export interface FinalsBlock {
  id: number;
  day: string;
  day_label: string;
  ordinal: number;
  start_time: string;
  end_time: string;
  label: string;
}

export interface FinalsAssignment {
  section_id: number;
  crn: string;
  course_code: string;
  section_number: string;
  block_id: number | null;
  block_label: string | null;
  room_id: string;
  is_in_place: boolean;
  in_place_time_slot_name: string | null;
}

export interface FinalsScheduleDetail {
  id: number;
  schedule_id: number;
  name: string;
  created_at: string;
  status: string;
  config: Record<string, unknown>;
  solver_stats: FinalsSolverStats;
  blocks: FinalsBlock[];
  assignments: FinalsAssignment[];
}

export interface FinalsSolveResponse {
  finals_schedule_id: number;
  status: string;
  objective_value: number;
  solve_time_seconds: number;
  num_solver_sections: number;
  num_in_place_sections: number;
  num_skipped_sections: number;
  num_students_with_conflict: number;
  num_block_conflicts: number;
}

export interface FinalsConflictStudent {
  student_id: string;
  clashes: { block: string; sections: string[] }[];
}

// Shape of FinalsScheduleDetail.solver_stats (written by the finals solver;
// all keys optional because older runs may predate some of them).
export interface FinalsSolverStats {
  status?: string;
  objective?: number;
  solve_time_seconds?: number;
  num_solver_sections?: number;
  num_in_place_sections?: number;
  num_skipped_sections?: number;
  num_blocks?: number;
  num_students_with_conflict?: number;
  num_block_conflicts?: number;
  conflict_students?: FinalsConflictStudent[];
  conflict_students_truncated?: boolean;
  level_prior_fallback_used?: boolean;
}

export interface ImportKindInfo {
  key: string;
  label: string;
  filename: string;
  description: string;
  is_yaml: boolean;
}

export interface ImportFileResponse {
  kind: string;
  filename: string;
  rows_loaded: number;
  cleared: boolean;
  errors: string[];
}

export interface ConflictPair {
  course1: string;
  course2: string;
  conflict_score: number;
}

export interface ConflictMatrixResponse {
  total_courses: number;
  high_conflict_pairs: ConflictPair[];
  threshold: number;
}

export interface CourseInfo {
  code: string;
  title: string;
  credits: number;
  structure_type: string;
  required_room_types: string[];
  valid_slot_types: string[];
  offer_terms: string[];
  cadence: string;
}

export interface StudentInfo {
  id: string;
  name: string;
  class_standing: string;
  major: string;
  credits_earned: number;
}

export interface FacultyInfo {
  id: string;
  name: string;
  email: string;
  max_credits: number;
  approved_courses: string[];
  unavailable_slots: number[];
  preferences: Record<string, unknown>;
}

export interface RoomInfo {
  id: string;
  building: string;
  number: string;
  capacity: number;
  room_type: string;
}

export interface TimeSlotInfo {
  id: number;
  name: string;
  pattern: string;
  days: string[];
  start_time: string;
  end_time: string;
  duration_minutes: number;
  slot_type: string;
}

export interface ScheduleDiffEntry {
  course_code: string;
  section_number: string;
  crn?: string;
  slot?: string | null;
  room?: string | null;
  instructor?: string | null;
  pinned?: boolean;
  changes?: Record<string, { from: string | null; to: string | null }>;
}

export interface ScheduleDiff {
  schedule: { id: number; name: string };
  base: { id: number; name: string };
  added: ScheduleDiffEntry[];
  removed: ScheduleDiffEntry[];
  changed: ScheduleDiffEntry[];
  unchanged_count: number;
}

export interface OptimizationStatus {
  running: boolean;
  kind?: string; // "generate" | "re-solve" | "finals"
  label?: string;
  started_at?: string;
}

export interface ProjectionInfo {
  id: number;
  major: string;
  estimated_count: number;
  catalog_year: number;
  semester: string;
  is_active: boolean;
}

export interface ProjectionCreateRequest {
  major: string;
  estimated_count: number;
  catalog_year: number;
  semester?: string;
}

export interface InferOfferingsResponse {
  courses_examined: number;
  courses_with_history: number;
  courses_changed: number;
  schedules_examined: number;
  skipped_unparseable_semesters: string[];
  changes: {
    course_code: string;
    observed_terms: string[];
    observed_years: number[];
    new_offer_terms: string[];
    new_cadence: string;
    note: string | null;
  }[];
}

// ─────────────────────────────────────────────────────────────────
// Endpoints
// ─────────────────────────────────────────────────────────────────

export const api = {
  stats: () => request<SystemStats>("/data/stats"),

  // Single-user optimization guard — is a solve running right now?
  explainSection: (scheduleId: number, sectionId: number) =>
    request<{ section_id: number; crn: string; reasons: string[] }>(
      `/schedule/${scheduleId}/sections/${sectionId}/explain`
    ),
  diffSchedules: (scheduleId: number, baseId: number) =>
    request<ScheduleDiff>(`/schedule/${scheduleId}/diff/${baseId}`),
  apiRoot: () =>
    request<{ name: string; version: string; auth_required: boolean }>(""),
  optimizationStatus: () =>
    request<OptimizationStatus>("/system/optimization-status"),

  listSchedules: () => request<ScheduleSummary[]>("/schedule/"),
  getSchedule: (id: number) => request<ScheduleDetail>(`/schedule/${id}`),
  pinSection: (scheduleId: number, sectionId: number, pinReason = "Pinned via web") =>
    request<SectionPinResponse>(`/schedule/${scheduleId}/sections/${sectionId}/pin`, {
      method: "PATCH",
      body: JSON.stringify({ pin_reason: pinReason }),
    }),
  unpinSection: (scheduleId: number, sectionId: number) =>
    request<SectionPinResponse>(`/schedule/${scheduleId}/sections/${sectionId}/pin`, {
      method: "DELETE",
    }),
  // Manual edit = pin with explicit assignments (faculty/time/room).
  editSection: (
    scheduleId: number,
    sectionId: number,
    body: { faculty_id?: string; time_slot_id?: number; room_id?: string }
  ) =>
    request<SectionPinResponse>(`/schedule/${scheduleId}/sections/${sectionId}/pin`, {
      method: "PATCH",
      body: JSON.stringify({ ...body, pin_reason: "Manually edited" }),
    }),
  checkConflicts: (scheduleId: number) =>
    request<CheckConflictsResponse>(`/schedule/${scheduleId}/check-conflicts`),
  scheduleViability: (scheduleId: number) =>
    request<ViabilityReport>(`/schedule/${scheduleId}/viability`),
  scheduleStudentQuality: (scheduleId: number) =>
    request<StudentQualityReport>(`/schedule/${scheduleId}/student-quality`),
  reSolveStreamUrl: (scheduleId: number, params: { name?: string; time_limit?: number }) => {
    const qs = new URLSearchParams();
    if (params.name) qs.set("name", params.name);
    if (params.time_limit) qs.set("time_limit", String(params.time_limit));
    return fileUrl(`/schedule/${scheduleId}/re-solve/stream?${qs.toString()}`);
  },
  repairStreamUrl: (scheduleId: number, params: { name?: string; time_limit?: number }) => {
    const qs = new URLSearchParams();
    if (params.name) qs.set("name", params.name);
    if (params.time_limit) qs.set("time_limit", String(params.time_limit));
    return fileUrl(`/schedule/${scheduleId}/repair/stream?${qs.toString()}`);
  },
  generateStreamUrl: (params: {
    semester: string;
    name: string;
    time_limit: number;
    include_projections?: boolean;
    class_size_cap?: number;
    capacity_mode?: "fixed" | "room";
    conflict_threshold?: number;
  }) => {
    const qs = new URLSearchParams({
      semester: params.semester,
      name: params.name,
      time_limit: String(params.time_limit),
    });
    if (params.include_projections) qs.set("include_projections", "true");
    if (params.class_size_cap) qs.set("class_size_cap", String(params.class_size_cap));
    if (params.capacity_mode) qs.set("capacity_mode", params.capacity_mode);
    if (params.conflict_threshold != null)
      qs.set("conflict_threshold", String(params.conflict_threshold));
    return fileUrl(`/schedule/generate/stream?${qs.toString()}`);
  },

  listFinalsForSchedule: (scheduleId: number) =>
    request<FinalsScheduleSummary[]>(`/finals/by-schedule/${scheduleId}`),
  getFinals: (finalsId: number) => request<FinalsScheduleDetail>(`/finals/${finalsId}`),
  solveFinals: (scheduleId: number, body: { name: string; config?: Record<string, unknown> }) =>
    request<FinalsSolveResponse>(`/finals/${scheduleId}/solve`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteFinals: (finalsId: number) =>
    request<{ message: string }>(`/finals/${finalsId}`, { method: "DELETE" }),
  finalsCsvUrl: (finalsId: number) => fileUrl(`/finals/${finalsId}/export.csv`),

  // Data file import
  importKinds: () => request<ImportKindInfo[]>("/data/import/kinds"),
  importFile: (kind: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    const token = getToken();
    // Don't set Content-Type; the browser adds the multipart boundary.
    // The Authorization header must still be attached — without it every
    // upload 401s in hardened (--secure) mode.
    return fetch(`${API_BASE}/data/import/${kind}`, {
      method: "POST",
      body: form,
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    }).then(
      async (res) => {
        if (!res.ok) {
          let detail = res.statusText;
          try {
            const body = await res.json();
            if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
          } catch {
            // keep statusText
          }
          throw new ApiError(res.status, detail);
        }
        return (await res.json()) as ImportFileResponse;
      }
    );
  },

  // Conflicts
  conflicts: (threshold = 50, limit = 100) =>
    request<ConflictMatrixResponse>(`/conflicts/?threshold=${threshold}&limit=${limit}`),

  // Data browser
  listCourses: (limit = 500) => request<CourseInfo[]>(`/data/courses?limit=${limit}`),
  listStudents: (limit = 500) => request<StudentInfo[]>(`/data/students?limit=${limit}`),
  listFaculty: () => request<FacultyInfo[]>("/data/faculty"),
  listRooms: () => request<RoomInfo[]>("/data/rooms"),
  listTimeSlots: () => request<TimeSlotInfo[]>("/data/timeslots"),
  inferOfferings: (apply = true) =>
    request<InferOfferingsResponse>("/data/courses/infer-offerings", {
      method: "POST",
      body: JSON.stringify({ apply }),
    }),

  // Freshmen enrollment projections
  listProjections: () => request<ProjectionInfo[]>("/projections/"),
  createProjection: (body: ProjectionCreateRequest) =>
    request<ProjectionInfo>("/projections/", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteProjection: (id: number) =>
    request<{ message: string }>(`/projections/${id}`, {
      method: "DELETE",
    }),
};
