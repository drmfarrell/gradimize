import type { ViabilityReport } from "../../api";

// Section-viability report: which sections are below minimum enrollment, and
// who gets stranded if each is cancelled (seat- and time-conflict-aware).
export function ViabilityCard({
  report,
  onClose,
}: {
  report: ViabilityReport;
  onClose: () => void;
}) {
  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <div className="row">
        <strong>
          {report.below_min.length
            ? `${report.below_min.length} of ${report.total_sections} sections below minimum enrollment`
            : `All ${report.total_sections} sections meet their minimum enrollment`}
        </strong>
        <div className="spacer" />
        <button className="ghost" onClick={onClose}>
          Close
        </button>
      </div>
      {report.below_min.map((sv) => (
        <div key={sv.section_id} style={{ marginTop: 10 }}>
          <strong>
            {sv.course_code} {sv.section_number}
          </strong>{" "}
          <span className="subtle">
            CRN {sv.crn} — {sv.enrolled} enrolled (minimum {sv.min_enrollment})
          </span>
          <div
            style={{
              color: sv.stranded_grad_critical
                ? "var(--red, #d24)"
                : sv.stranded
                  ? "var(--yellow, #b80)"
                  : "var(--green, #2a4)",
            }}
          >
            {sv.recommendation}
          </div>
          {sv.stranded > 0 && sv.stranded_students.length > 0 && (
            <div className="subtle">
              Stranded: {sv.stranded_students.join(", ")}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
