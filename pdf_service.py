"""
PDF Generation Service.
Generates resumes in PDF format.
Uses WeasyPrint (with HTML/CSS templates) when available.
Falls back to a robust built-in FPDF generator if WeasyPrint native libraries are missing.
"""
import os
import re
import html
import base64
import logging
from io import BytesIO

logger = logging.getLogger(__name__)

# Check WeasyPrint availability
_weasyprint_available = False
_WeasyHTML = None

try:
    from weasyprint import HTML as _WeasyHTML
    _weasyprint_available = True
except Exception as e:
    logger.info(f"WeasyPrint native libraries not loaded ({e}). Fallback FPDF generator will be used if needed.")


def clean_html_text(html_str):
    """Convert HTML snippet from Quill editor into clean readable text."""
    if not html_str:
        return ""
    text = html_str
    # Replace line breaks and paragraph tags with newlines
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</li>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<li[^>]*>', '• ', text, flags=re.IGNORECASE)
    text = re.sub(r'<h[1-6][^>]*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</h[1-6]>', '\n', text, flags=re.IGNORECASE)
    # Strip remaining HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Decode HTML entities
    text = html.unescape(text)
    # Collapse excess blank lines
    lines = [line.strip() for line in text.split('\n')]
    text = '\n'.join([l for l in lines if l])
    return text


def _generate_fpdf(personal, prof):
    """Generate resume PDF using FPDF as a lightweight fallback."""
    import fpdf

    class ResumePDF(fpdf.FPDF):
        def header(self):
            pass

        def footer(self):
            self.set_y(-15)
            self.set_font('Arial', 'I', 8)
            self.set_text_color(128, 128, 128)
            self.cell(0, 10, f'Page {self.page_no()}', 0, 0, 'C')

    pdf = ResumePDF(format='A4', unit='mm')
    pdf.set_margins(18, 18, 18)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    def safe_text(txt):
        if not txt:
            return ""
        # Encode as latin-1 safe string for fpdf
        return str(txt).encode('latin-1', 'replace').decode('latin-1')

    # Header: Name
    name = personal.get('name', 'Resume').strip()
    pdf.set_font('Arial', 'B', 18)
    pdf.set_text_color(17, 24, 39)
    pdf.cell(0, 9, safe_text(name), 0, 1, 'C')

    # Contact line
    contacts = []
    if personal.get('email'):
        contacts.append(personal.get('email'))
    if personal.get('phone'):
        contacts.append(personal.get('phone'))
    if personal.get('location'):
        contacts.append(personal.get('location'))
    if personal.get('linkedin'):
        contacts.append(personal.get('linkedin'))
    if personal.get('github'):
        contacts.append(personal.get('github'))
    if personal.get('portfolio'):
        contacts.append(personal.get('portfolio'))

    if contacts:
        pdf.set_font('Arial', '', 9)
        pdf.set_text_color(75, 85, 99)
        pdf.multi_cell(0, 5, safe_text(' | '.join(contacts)), 0, 'C')

    pdf.ln(3)

    def draw_section_heading(title):
        pdf.ln(2)
        pdf.set_font('Arial', 'B', 11)
        pdf.set_text_color(31, 41, 55)
        pdf.cell(0, 6, safe_text(title.upper()), 0, 1, 'L')
        pdf.set_draw_color(209, 213, 219)
        pdf.set_line_width(0.3)
        pdf.line(pdf.get_x(), pdf.get_y(), pdf.w - 18, pdf.get_y())
        pdf.ln(2)

    # 1. Summary
    summary = clean_html_text(prof.get('summary', ''))
    if summary:
        draw_section_heading('Professional Summary')
        pdf.set_font('Arial', '', 9.5)
        pdf.set_text_color(55, 65, 81)
        pdf.multi_cell(0, 4.5, safe_text(summary))
        pdf.ln(1)

    # 2. Education
    education = prof.get('education', [])
    if education:
        draw_section_heading('Education')
        for edu in education:
            inst = edu.get('institution', '')
            deg = edu.get('degree', '')
            dates = edu.get('dates', '')
            score = edu.get('score', '')

            pdf.set_font('Arial', 'B', 9.5)
            pdf.set_text_color(17, 24, 39)
            pdf.cell(120, 5, safe_text(inst), 0, 0, 'L')
            pdf.set_font('Arial', 'I', 9)
            pdf.set_text_color(107, 114, 128)
            pdf.cell(0, 5, safe_text(dates), 0, 1, 'R')

            line2 = deg
            if score:
                line2 += f" - Score: {score}"
            if line2:
                pdf.set_font('Arial', '', 9)
                pdf.set_text_color(75, 85, 99)
                pdf.cell(0, 4.5, safe_text(line2), 0, 1, 'L')
            pdf.ln(1)

    # 3. Work Experience
    experience = prof.get('work_experience', [])
    if experience:
        draw_section_heading('Experience')
        for exp in experience:
            company = exp.get('company', '')
            role = exp.get('role', '')
            dates = exp.get('dates', '')
            desc = clean_html_text(exp.get('description', ''))

            pdf.set_font('Arial', 'B', 9.5)
            pdf.set_text_color(17, 24, 39)
            pdf.cell(120, 5, safe_text(company), 0, 0, 'L')
            pdf.set_font('Arial', 'I', 9)
            pdf.set_text_color(107, 114, 128)
            pdf.cell(0, 5, safe_text(dates), 0, 1, 'R')

            if role:
                pdf.set_font('Arial', 'I', 9)
                pdf.set_text_color(75, 85, 99)
                pdf.cell(0, 4.5, safe_text(role), 0, 1, 'L')

            if desc:
                pdf.set_font('Arial', '', 9)
                pdf.set_text_color(55, 65, 81)
                pdf.multi_cell(0, 4.5, safe_text(desc))
            pdf.ln(1)

    # 4. Technical & Non-Technical Skills
    tech_skills = clean_html_text(prof.get('technical_skills', ''))
    non_tech_skills = clean_html_text(prof.get('non_technical_skills', ''))
    if tech_skills or non_tech_skills:
        draw_section_heading('Skills')
        if tech_skills:
            pdf.set_font('Arial', 'B', 9)
            pdf.set_text_color(31, 41, 55)
            pdf.cell(0, 4.5, safe_text('Technical Skills:'), 0, 1, 'L')
            pdf.set_font('Arial', '', 9)
            pdf.set_text_color(55, 65, 81)
            pdf.multi_cell(0, 4.5, safe_text(tech_skills))
            pdf.ln(1)
        if non_tech_skills:
            pdf.set_font('Arial', 'B', 9)
            pdf.set_text_color(31, 41, 55)
            pdf.cell(0, 4.5, safe_text('Non-Technical Skills:'), 0, 1, 'L')
            pdf.set_font('Arial', '', 9)
            pdf.set_text_color(55, 65, 81)
            pdf.multi_cell(0, 4.5, safe_text(non_tech_skills))
            pdf.ln(1)

    # 5. Projects
    projects = prof.get('projects', [])
    if projects:
        draw_section_heading('Projects')
        for prj in projects:
            pname = prj.get('name', '')
            plink = prj.get('link', '')
            pdesc = clean_html_text(prj.get('description', ''))

            pdf.set_font('Arial', 'B', 9.5)
            pdf.set_text_color(17, 24, 39)
            pdf.cell(0, 5, safe_text(pname), 0, 1, 'L')
            if plink:
                pdf.set_font('Arial', '', 8.5)
                pdf.set_text_color(59, 130, 246)
                pdf.cell(0, 4, safe_text(plink), 0, 1, 'L')
            if pdesc:
                pdf.set_font('Arial', '', 9)
                pdf.set_text_color(55, 65, 81)
                pdf.multi_cell(0, 4.5, safe_text(pdesc))
            pdf.ln(1)

    # 6. Certifications
    certs = clean_html_text(prof.get('certifications', ''))
    if certs:
        draw_section_heading('Certifications')
        pdf.set_font('Arial', '', 9)
        pdf.set_text_color(55, 65, 81)
        pdf.multi_cell(0, 4.5, safe_text(certs))
        pdf.ln(1)

    # 7. Achievements & Awards
    achievements = clean_html_text(prof.get('achievements', ''))
    if achievements:
        draw_section_heading('Achievements & Awards')
        pdf.set_font('Arial', '', 9)
        pdf.set_text_color(55, 65, 81)
        pdf.multi_cell(0, 4.5, safe_text(achievements))
        pdf.ln(1)

    # 8. Extracurricular
    extra = clean_html_text(prof.get('extracurricular_activities', ''))
    if extra:
        draw_section_heading('Extracurricular Activities')
        pdf.set_font('Arial', '', 9)
        pdf.set_text_color(55, 65, 81)
        pdf.multi_cell(0, 4.5, safe_text(extra))

    raw_str = pdf.output(dest='S')
    return raw_str.encode('latin1')


def generate_resume_pdf(personal, prof, render_template_fn=None, host_url='http://localhost:5003/'):
    """
    Generate PDF bytes.
    Tries WeasyPrint first; if unavailable, uses the FPDF fallback generator.
    """
    if _weasyprint_available and _WeasyHTML is not None and render_template_fn:
        try:
            icons = {}
            static_icons_path = os.path.join(os.path.dirname(__file__), 'static', 'icons')
            for name in ['email', 'phone', 'location', 'linkedin', 'github', 'website']:
                icon_path = os.path.join(static_icons_path, f'{name}.png')
                if os.path.exists(icon_path):
                    with open(icon_path, "rb") as f:
                        icons[name] = base64.b64encode(f.read()).decode('utf-8')

            rendered_html = render_template_fn(
                'resume_pdf.html',
                personal=personal,
                prof=prof,
                icons=icons
            )
            pdf_bytes = _WeasyHTML(string=rendered_html, base_url=host_url).write_pdf()
            if pdf_bytes and len(pdf_bytes) > 100:
                return pdf_bytes
        except Exception as e:
            logger.warning(f"WeasyPrint rendering failed ({e}). Falling back to FPDF generator.")

    # Fallback to FPDF
    return _generate_fpdf(personal, prof)
