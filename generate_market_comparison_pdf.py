import os
import sys
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, HRFlowable
)
from reportlab.pdfgen import canvas

class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        self.setFont("Helvetica-Bold", 8)
        self.setFillColor(colors.HexColor("#64748b"))
        
        # Header (Pages > 1)
        if self._pageNumber > 1:
            self.drawString(36, 806, "LAMO HOME SERVICES MARKET RESEARCH | COMPETITIVE BENCHMARK & LIFECYCLE AUDIT")
            self.setStrokeColor(colors.HexColor("#cbd5e1"))
            self.setLineWidth(0.5)
            self.line(36, 800, 559, 800)
        
        # Footer
        self.setStrokeColor(colors.HexColor("#cbd5e1"))
        self.setLineWidth(0.5)
        self.line(36, 40, 559, 40)
        self.setFont("Helvetica", 8)
        self.drawString(36, 28, "Confidential - For Strategic Planning & Architecture Audit")
        self.drawRightString(559, 28, f"Page {self._pageNumber} of {page_count}")
        self.restoreState()

def build_pdf(filename="LAMO_Home_Services_Market_Comparison.pdf"):
    doc = SimpleDocTemplate(
        filename,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=50,
        bottomMargin=50
    )

    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=24,
        leading=28,
        textColor=colors.HexColor('#064e3b'),
        spaceAfter=6
    )
    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=12,
        leading=16,
        textColor=colors.HexColor('#334155'),
        spaceAfter=15
    )
    h1_style = ParagraphStyle(
        'Heading1Custom',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=15,
        leading=19,
        textColor=colors.HexColor('#0f172a'),
        spaceBefore=14,
        spaceAfter=8,
        keepWithNext=True
    )
    h2_style = ParagraphStyle(
        'Heading2Custom',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=colors.HexColor('#047857'),
        spaceBefore=10,
        spaceAfter=5,
        keepWithNext=True
    )
    body_style = ParagraphStyle(
        'BodyCustom',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9.5,
        leading=13.5,
        textColor=colors.HexColor('#1e293b'),
        spaceAfter=6
    )
    body_bold = ParagraphStyle(
        'BodyBoldCustom',
        parent=body_style,
        fontName='Helvetica-Bold'
    )
    callout_style = ParagraphStyle(
        'CalloutText',
        parent=styles['Normal'],
        fontName='Helvetica-Oblique',
        fontSize=9,
        leading=13,
        textColor=colors.HexColor('#065f46')
    )
    th_style = ParagraphStyle(
        'TableHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        textColor=colors.white,
        alignment=1
    )
    tb_style = ParagraphStyle(
        'TableCell',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=7.5,
        leading=10,
        textColor=colors.HexColor('#0f172a')
    )
    tb_bold = ParagraphStyle(
        'TableCellBold',
        parent=tb_style,
        fontName='Helvetica-Bold'
    )
    badge_style = ParagraphStyle(
        'BadgeText',
        parent=tb_style,
        fontName='Helvetica-Bold',
        fontSize=7,
        leading=9,
        textColor=colors.HexColor('#047857'),
        alignment=1
    )

    story = []

    # ==========================================
    # COVER / HEADER SECTION
    # ==========================================
    story.append(Paragraph("LAMO Home Services Marketplace", title_style))
    story.append(Paragraph("Deep Working Lifecycle Audit & Pan-India / Kerala Competitive Benchmark Report", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#059669'), spaceBefore=2, spaceAfter=14))

    meta_table_data = [
        [
            Paragraph("<b>Target Market:</b> Home & Utility Services (Kerala & India)", body_style),
            Paragraph("<b>Analyzed Platforms:</b> Urban Company, Joboy, Panikkaar, SmartWorker, NoBroker, Sulekha, Swiggy & LAMO", body_style)
        ],
        [
            Paragraph("<b>Audit Date:</b> November 2026", body_style),
            Paragraph("<b>Core Scope:</b> Discovery, Booking, Slot Scheduling, Worker Assignment, Transit, OTPs, Payments & Dispute Handshake", body_style)
        ]
    ]
    meta_table = Table(meta_table_data, colWidths=[240, 283])
    meta_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#f8fafc')),
        ('BOX', (0,0), (-1,-1), 1, colors.HexColor('#e2e8f0')),
        ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#e2e8f0')),
        ('PADDING', (0,0), (-1,-1), 6),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 14))

    # ==========================================
    # SECTION 1: EXECUTIVE SUMMARY
    # ==========================================
    story.append(Paragraph("1. Executive Summary & Market Archetypes", h1_style))
    exec_summary_text = (
        "The on-demand home services sector in India is divided into three distinct operational archetypes: "
        "<b>(1) The Lead Generation Directory</b> (e.g. Sulekha, JustDial, Panikkaar) which acts as an unmanaged phonebook, "
        "passing leads directly to local workers; <b>(2) The Fully Managed Escrow Super-App</b> (e.g. Urban Company, Joboy, NoBroker) "
        "which controls pricing, assigns workers through rigid algorithms, and takes 20-25% commissions; and "
        "<b>(3) The Instant Courier / Hyperlocal Logistics Model</b> (e.g. Swiggy Genie/Instapronto) which excels at speed "
        "but is structurally ill-suited for complex physical trades like electrical, plumbing, or AC repair.<br/><br/>"
        "<b>LAMO occupies a powerful hybrid sweet-spot:</b> It gives customers the <i>personal agency to choose verified local "
        "professionals directly</i> (preventing anonymous subcontracting) while maintaining the <i>rigorous booking lifecycle, "
        "concurrency locks, 30-minute smart scheduling, and 30-day service guarantees</i> found in managed platforms—all while "
        "avoiding customer OTP fatigue and high gateway fees through direct UPI/Cash settlements."
    )
    story.append(Paragraph(exec_summary_text, body_style))
    story.append(Spacer(1, 10))

    # Archetype breakdown callout box
    arch_data = [
        [
            Paragraph("<b>Directory Model</b><br/><font color='#64748b'>Panikkaar, Sulekha</font><br/>• Phone/WhatsApp direct<br/>• 0% booking commission<br/>• No booking schedule/slots<br/>• High no-show & haggle risk", tb_style),
            Paragraph("<b>Managed Super-App</b><br/><font color='#64748b'>Urban Company, Joboy</font><br/>• Auto-assigned worker<br/>• 20–25% heavy commission<br/>• Fixed package pricing<br/>• Dual OTPs (start + end)", tb_style),
            Paragraph("<b>LAMO Hybrid Model</b><br/><font color='#047857'><b>Best of Both Worlds</b></font><br/>• Transparent worker choice<br/>• 10% fair fee (0% launch)<br/>• 30m slots + transit buffers<br/>• 1 Doorstep PIN (Zero fatigue)", tb_style),
        ]
    ]
    arch_table = Table(arch_data, colWidths=[170, 170, 183])
    arch_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (0,0), colors.HexColor('#f1f5f9')),
        ('BACKGROUND', (1,0), (1,0), colors.HexColor('#fef3c7')),
        ('BACKGROUND', (2,0), (2,0), colors.HexColor('#ecfdf5')),
        ('BOX', (0,0), (-1,-1), 1, colors.HexColor('#cbd5e1')),
        ('INNERGRID', (0,0), (-1,-1), 1, colors.HexColor('#cbd5e1')),
        ('PADDING', (0,0), (-1,-1), 8),
    ]))
    story.append(arch_table)
    story.append(Spacer(1, 14))

    # ==========================================
    # SECTION 2: THE 10-STAGE LIFECYCLE MATRIX
    # ==========================================
    story.append(Paragraph("2. End-to-End Lifecycle Comparison Matrix", h1_style))
    story.append(Paragraph(
        "Below is a deep architectural teardown of how each platform executes every critical step from discovery to completion and dispute resolution.",
        body_style
    ))
    story.append(Spacer(1, 6))

    headers = [
        Paragraph("<b>Stage / Feature</b>", th_style),
        Paragraph("<b>Urban Company</b>", th_style),
        Paragraph("<b>Joboy (Kerala)</b>", th_style),
        Paragraph("<b>Panikkaar</b>", th_style),
        Paragraph("<b>NoBroker / Sulekha</b>", th_style),
        Paragraph("<b>LAMO (Our System)</b>", th_style)
    ]

    matrix_rows = [
        headers,
        [
            Paragraph("<b>1. Worker Selection</b>", tb_bold),
            Paragraph("Algorithmic auto-dispatch. Customer <i>cannot</i> choose worker.", tb_style),
            Paragraph("System assigns or telephone confirmation team allocates pro.", tb_style),
            Paragraph("Direct manual directory search via list & map radius.", tb_style),
            Paragraph("NoBroker assigns. Sulekha sells lead to 3-5 competing vendors.", tb_style),
            Paragraph("<b>Customer Chooses Pro</b> via ratings, experience, rate & distance + Emergency Pool.", tb_style),
        ],
        [
            Paragraph("<b>2. Time Slots</b>", tb_bold),
            Paragraph("1-hour to 2-hour broad windows. Partner dispatched dynamically.", tb_style),
            Paragraph("1-hour fixed slots (e.g. 10-11 AM, 2-3 PM).", tb_style),
            Paragraph("<b>No slots.</b> Offline negotiation via direct phone call.", tb_style),
            Paragraph("NoBroker: Morning/Afternoon. Sulekha: Unscheduled phone bids.", tb_style),
            Paragraph("<b>Dynamic 30-Min Slots</b> bounded by worker's shift + 1hr same-day lead time.", tb_style),
        ],
        [
            Paragraph("<b>3. Worker Acceptance</b>", tb_bold),
            Paragraph("Partner has 60–90s alarm ring to claim or auto-reassigned with penalty.", tb_style),
            Paragraph("Manual support confirmation or partner app acceptance (15–30m).", tb_style),
            Paragraph("N/A (Customer rings worker on mobile/WhatsApp).", tb_style),
            Paragraph("NoBroker: Partner confirms. Sulekha: Vendors call customer directly.", tb_style),
            Paragraph("<b>Tiered Timers:</b> 20m for Same-Day, 60m for Future + 1-Tap Auto-Reassign.", tb_style),
        ],
        [
            Paragraph("<b>4. Overrun & Transit Coordination</b>", tb_bold),
            Paragraph("GPS geofence tracks partner travel. Automated delay notifications.", tb_style),
            Paragraph("Customer support calls if partner reports transit delay.", tb_style),
            Paragraph("Zero tracking. Customer must phone worker to ask 'Where are you?'.", tb_style),
            Paragraph("Relationship manager updates customer via SMS/call if delayed.", tb_style),
            Paragraph("<b>Live Transit Context:</b> Worker selects 15/30m buffer or reschedule; customer delay banner.", tb_style),
        ],
        [
            Paragraph("<b>5. Doorstep Verification</b>", tb_bold),
            Paragraph("Start OTP required on partner app before unlocking work checklist.", tb_style),
            Paragraph("Worker checks in on mobile app; optional SMS PIN.", tb_style),
            Paragraph("Zero verification. Worker knocks and starts work.", tb_style),
            Paragraph("NoBroker: Digital check-in. Sulekha: Completely unverified.", tb_style),
            Paragraph("<b>4-Digit Arrival PIN:</b> Verified on doorstep before start. Early arrival consent bypass.", tb_style),
        ],
        [
            Paragraph("<b>6. Completion Verification</b>", tb_bold),
            Paragraph("<b>Completion OTP:</b> Partner enters customer SMS code to close job.", tb_style),
            Paragraph("Customer signs off on partner app or support marks complete.", tb_style),
            Paragraph("Zero verification. Worker packs tools and asks for cash.", tb_style),
            Paragraph("Job closed on vendor app or automated post-service follow-up.", tb_style),
            Paragraph("<b>Zero PIN Fatigue:</b> Worker submits itemized bill; customer 1-tap confirms + 48h auto-close.", tb_style),
        ],
        [
            Paragraph("<b>7. Billing & Spare Parts</b>", tb_bold),
            Paragraph("Pre-fixed rate card. Extra parts billed via app with company markup.", tb_style),
            Paragraph("Minimum inspection fee (₹199-299) + standard part rate card.", tb_style),
            Paragraph("Unregulated verbal haggling. Worker quotes arbitrary price.", tb_style),
            Paragraph("NoBroker: Detailed estimate. Sulekha: Unstandardized vendor quotes.", tb_style),
            Paragraph("<b>Itemized Split:</b> Labor + Spare Parts shown clearly. Bill locked until job finished.", tb_style),
        ],
        [
            Paragraph("<b>8. Payment Methods</b>", tb_bold),
            Paragraph("In-app online gateway (Cards, Netbanking, UPI, UC Wallet).", tb_style),
            Paragraph("Online pre-payment or Cash/UPI collected by technician.", tb_style),
            Paragraph("Direct Cash or worker's personal GPay/PhonePe offline.", tb_style),
            Paragraph("Online payment to platform or Cash on Delivery.", tb_style),
            Paragraph("<b>Direct to Worker:</b> Dynamic personal UPI QR + Cash (Zero gateway cut for small jobs).", tb_style),
        ],
        [
            Paragraph("<b>9. Platform Commission</b>", tb_bold),
            Paragraph("<b>20% to 25%</b> on total bill + mandatory partner tool kit sales.", tb_style),
            Paragraph("<b>15% to 20%</b> commission deducted from worker balance.", tb_style),
            Paragraph("<b>0% commission:</b> Workers pay ₹500–₹1500 annual directory listing.", tb_style),
            Paragraph("NoBroker: 15–20% margin. Sulekha: ₹50–₹300 per lead sold.", tb_style),
            Paragraph("<b>Fair 10% Fee:</b> Deducted from wallet ONLY on labor (0% on parts, 0% during launch).", tb_style),
        ],
        [
            Paragraph("<b>10. Guarantee & Disputes</b>", tb_bold),
            Paragraph("30-day service guarantee + up to ₹10,000 damage insurance cover.", tb_style),
            Paragraph("7 to 30 days re-work guarantee by Joboy support team.", tb_style),
            Paragraph("<b>Zero guarantee:</b> Platform takes no responsibility once number shared.", tb_style),
            Paragraph("NoBroker: Re-service guarantee. Sulekha: Zero guarantee or liability.", tb_style),
            Paragraph("<b>30-Day Guarantee:</b> 1-tap Dispute button creates High-Severity Admin arbitration.", tb_style),
        ],
    ]

    col_widths = [80, 88, 88, 88, 88, 91]
    matrix_table = Table(matrix_rows, colWidths=col_widths, repeatRows=1)
    matrix_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#064e3b')),
        ('BACKGROUND', (5,1), (5,-1), colors.HexColor('#f0fdf4')), # Highlight LAMO
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#cbd5e1')),
        ('PADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(matrix_table)
    story.append(Spacer(1, 14))

    story.append(PageBreak())

    # ==========================================
    # SECTION 3: PLATFORM-BY-PLATFORM TEARDOWN
    # ==========================================
    story.append(Paragraph("3. Detailed Platform Teardowns & Architecture Analysis", h1_style))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#059669'), spaceBefore=2, spaceAfter=10))

    # 1. Urban Company
    story.append(Paragraph("Platform 1: Urban Company (UC) — The Full-Stack Managed Titan", h2_style))
    uc_text = (
        "<b>Model:</b> Full-stack, vertically integrated managed marketplace.<br/>"
        "<b>Customer Flow:</b> Customer browses categorized service packages (e.g. 'Ceiling Fan Installation ₹149') $\\rightarrow$ "
        "Selects a broad 1-2 hour arrival window $\\rightarrow$ Pays online or selects Pay After Service $\\rightarrow$ "
        "UC backend algorithmically matches the nearest gig partner $\\rightarrow$ Partner arrives with branded kit $\\rightarrow$ "
        "Customer shares Start OTP $\\rightarrow$ Work performed $\\rightarrow$ Customer shares Completion OTP $\\rightarrow$ 30-day warranty.<br/>"
        "<b>Strengths:</b> Industry-best consistency, strict background verification, standardized tools, strong 30-day insurance.<br/>"
        "<b>Weaknesses & Partner Grievances:</b> Heavy 20–25% commission plus mandatory purchases of UC-branded uniforms and chemical kits. "
        "Customers cannot build personal rapport or choose the same technician again. Partners frequently strike against high penalties."
    )
    story.append(Paragraph(uc_text, body_style))
    story.append(Spacer(1, 8))

    # 2. Joboy
    story.append(Paragraph("Platform 2: Joboy — Kerala's Pioneer Managed Home Service Super-App", h2_style))
    joboy_text = (
        "<b>Model:</b> Kerala-born aggregator covering Kochi, Trivandrum, Kozhikode, and expanded to UAE & UK.<br/>"
        "<b>Customer Flow:</b> Web / Mobile app catalog $\\rightarrow$ Selects date & 1-hour slot $\\rightarrow$ "
        "Joboy operations desk reviews booking and dispatches vetted local trade partner $\\rightarrow$ Minimum inspection fee "
        "(₹199–₹299) applies if customer declines work after diagnosis $\\rightarrow$ Job completed $\\rightarrow$ Warranty issued.<br/>"
        "<b>Strengths:</b> Built specifically for Kerala's decentralized geography; strong customer support desk in Malayalam and English.<br/>"
        "<b>Weaknesses:</b> Heavily reliant on manual operations desk confirmation in smaller towns; app UI can feel cluttered due to "
        "trying to be an 'Everything App' (cake delivery, cleaning, salon, plumbing all mixed together)."
    )
    story.append(Paragraph(joboy_text, body_style))
    story.append(Spacer(1, 8))

    # 3. Panikkaar
    story.append(Paragraph("Platform 3: Panikkaar.com — Kerala's Direct Hyperlocal Directory", h2_style))
    panikkaar_text = (
        "<b>Model:</b> Peer-to-peer directory and zero-commission worker contact board.<br/>"
        "<b>Customer Flow:</b> Customer opens website/app $\\rightarrow$ Selects trade (Plumber, Electrician, Painter) $\\rightarrow$ "
        "Enters location/GPS $\\rightarrow$ App displays list of nearby registered workers with direct phone numbers & WhatsApp links $\\rightarrow$ "
        "Customer calls worker directly and negotiates time and rate offline.<br/>"
        "<b>Strengths:</b> 100% free for customers; workers keep 100% of money; hyper-local connectivity in rural Kerala.<br/>"
        "<b>Weaknesses:</b> <i>Zero quality control, zero scheduling, zero price protection, and zero dispute resolution.</i> "
        "Workers frequently fail to show up, quote inflated rates upon arrival, and the platform offers no warranty if work is defective."
    )
    story.append(Paragraph(panikkaar_text, body_style))
    story.append(Spacer(1, 8))

    # 4. SmartWorker.in
    story.append(Paragraph("Platform 4: SmartWorker.in — Regional Blue-Collar Service Portal", h2_style))
    smart_text = (
        "<b>Model:</b> Localized utility repair aggregator and lead matching network.<br/>"
        "<b>Customer Flow:</b> Customer submits requirement $\\rightarrow$ Portal matches verified local tradesman $\\rightarrow$ "
        "Technician visits location for inspection $\\rightarrow$ Standard service charges applied.<br/>"
        "<b>Strengths:</b> Focus on core blue-collar utilities (appliances, wiring, plumbing); verified local technicians.<br/>"
        "<b>Weaknesses:</b> Limited automated booking features; lacking real-time schedule conflict coordination or dynamic arrival tracking."
    )
    story.append(Paragraph(smart_text, body_style))
    story.append(Spacer(1, 8))

    # 5. NoBroker Home Services
    story.append(Paragraph("Platform 5: NoBroker Home Services — Move-in / Property Ecosystem Model", h2_style))
    nobroker_text = (
        "<b>Model:</b> Managed B2C home maintenance bundled with residential rental/society management.<br/>"
        "<b>Customer Flow:</b> Dedicated focus on high-ticket services (Deep cleaning, wall painting, pest control, packers & movers) $\\rightarrow$ "
        "Customer books via app $\\rightarrow$ Dedicated Relationship Manager coordinates verified sub-contractor team $\\rightarrow$ "
        "Lowest price guarantee with free cancellation before dispatch.<br/>"
        "<b>Strengths:</b> High-ticket quality, rigorous vendor vetting, integrated with society security gates.<br/>"
        "<b>Weaknesses:</b> Poor fit for small, instant emergency repair jobs (e.g. single tap leaking or switchboard sparking). High minimum order values."
    )
    story.append(Paragraph(nobroker_text, body_style))
    story.append(Spacer(1, 8))

    # 6. Sulekha
    story.append(Paragraph("Platform 6: Sulekha — The High-Friction Lead Generation Bazaar", h2_style))
    sulekha_text = (
        "<b>Model:</b> Paid lead broadcast auction.<br/>"
        "<b>Customer Flow:</b> Customer fills out a 5-step form describing need $\\rightarrow$ Sulekha instantly sells customer's phone number "
        "to 3 to 5 local contractors $\\rightarrow$ Customer receives 5 aggressive telemarketing calls within 15 minutes $\\rightarrow$ "
        "Customer selects whichever contractor sounds best.<br/>"
        "<b>Strengths:</b> Massive nationwide vendor coverage.<br/>"
        "<b>Weaknesses:</b> <b>Terrible user experience.</b> Customers hate receiving unwanted phone calls. Once leads are sold, Sulekha "
        "has zero involvement in service quality, pricing, safety, or guarantees."
    )
    story.append(Paragraph(sulekha_text, body_style))
    story.append(Spacer(1, 8))

    # 7. Swiggy
    story.append(Paragraph("Platform 7: Swiggy (Genie / Hyperlocal Delivery Framework)", h2_style))
    swiggy_text = (
        "<b>Model:</b> High-velocity point-to-point courier and delivery runner model.<br/>"
        "<b>Relevance to Home Services:</b> While Swiggy occasionally pilots local errands (Swiggy Genie for pick-up/drop-off), "
        "its dispatch algorithm is optimized for <b>15-30 minute delivery trips</b>, not scheduled skilled labor. Skilled home trades "
        "require diagnostic time, variable durations, material procurement, and technical licensing which delivery platforms cannot manage."
    )
    story.append(Paragraph(swiggy_text, body_style))
    story.append(Spacer(1, 14))

    story.append(PageBreak())

    # ==========================================
    # SECTION 4: THE LAMO ARCHITECTURAL BLUEPRINT
    # ==========================================
    story.append(Paragraph("4. The LAMO Competitive Advantage & Architectural Superiority", h1_style))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#059669'), spaceBefore=2, spaceAfter=10))

    lamo_intro = (
        "By studying the friction points of Urban Company, Panikkaar, and Sulekha, <b>LAMO was engineered from the ground up "
        "to solve the three biggest flaws in existing home service apps</b>: (1) Partner exploitation, (2) Customer OTP exhaustion, "
        "and (3) Schedule overruns."
    )
    story.append(Paragraph(lamo_intro, body_style))
    story.append(Spacer(1, 8))

    pillars = [
        ("Pillar 1: Transparent Worker Agency (Better than Urban Company)",
         "In Urban Company, customers cannot see or choose their worker. A random stranger is dispatched. "
         "In LAMO, customers browse verified professionals with full transparency: photos, customer ratings, years of trade experience, "
         "hourly rate, and exact working hours. This allows great workers to build repeat customer loyalty while giving customers peace of mind."),
        
        ("Pillar 2: Zero Customer OTP Fatigue (Better than Industry Standard)",
         "Most platforms require an Arrival OTP, a Mid-Job OTP, and a Completion OTP. Customers standing at their door find this annoying. "
         "LAMO enforces <b>strictly 1 PIN: The Doorstep Arrival PIN</b> to prove physical attendance. At job end, there are <b>NO CODES</b>: "
         "the worker submits the itemized bill, the customer taps 'Confirm Paid' in 1 second, and our 48-hour silent auto-close protects workers if customers forget."),
        
        ("Pillar 3: Smart Transit & Overrun Coordination (First in Industry)",
         "In typical apps, if Job 1 runs late, Job 2 fails silently. In LAMO, our physical concurrency locking blocks a worker from starting Job 2 while Job 1 is active. "
         "When Job 1 finishes, the worker gets an instant 3-option banner ('On Time', 'Need 15m Buffer', 'Propose Reschedule'), and Customer 2 gets a live courtesy banner. "
         "Nobody is left guessing."),

        ("Pillar 4: Fair 10% Labor Fee & Direct UPI QR (Better for Partners)",
         "Urban Company takes 25% of everything (including materials). LAMO charges only <b>10% on labor</b> (0% during launch, 0% on spare parts). "
         "Payments go <b>directly into the worker's personal UPI QR code or cash</b>, eliminating expensive gateway merchant processing cuts on small daily repairs."),

        ("Pillar 5: Tiered Response Timers & 1-Tap Auto-Reassign",
         "Instead of leaving customers waiting for hours, LAMO enforces a <b>20-minute timer for same-day bookings</b> and a <b>60-minute timer for future dates</b> "
         "(with night-safe pause from 9 PM to 8 AM). If a worker does not respond, the customer gets a 1-tap 'Auto-Reassign to Next Top Pro' button that instantly transfers the job.")
    ]

    for title, desc in pillars:
        story.append(Paragraph(f"<b>{title}</b>", h2_style))
        story.append(Paragraph(desc, body_style))
        story.append(Spacer(1, 4))

    story.append(Spacer(1, 10))

    # ==========================================
    # SECTION 5: KERALA REGIONAL NUANCES
    # ==========================================
    story.append(Paragraph("5. Kerala Market Nuances: Why Generic Apps Fail Here", h1_style))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#059669'), spaceBefore=2, spaceAfter=8))
    
    kerala_text = (
        "Kerala has unique socio-geographical characteristics that break traditional metro-centric models:<br/>"
        "• <b>The 'Continuous City' Landscape:</b> Unlike Bangalore or Mumbai which have dense high-rise clusters, Kerala is an unbroken "
        "semi-urban network of towns, panchayats, and independent houses. Transit distance between jobs is higher, making LAMO's "
        "<b>dynamic travel buffer scaling (15m/30m/45m based on km)</b> essential.<br/>"
        "• <b>High Consumer Rights & Social Trust:</b> Keralites prioritize background-checked, reputable local professionals. "
        "Unregulated directories like Panikkaar suffer from safety concerns, while fully anonymous dispatchers like Urban Company face resistance. "
        "LAMO's verified profile badges and transparent direct booking strike the exact cultural balance needed for long-term dominance."
    )
    story.append(Paragraph(kerala_text, body_style))
    story.append(Spacer(1, 14))

    # Strategic Conclusion
    story.append(Paragraph("6. Summary Recommendation for LAMO Production Launch", h1_style))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#059669'), spaceBefore=2, spaceAfter=8))
    conclusion_text = (
        "LAMO's architecture successfully avoids the customer-hostile spam of lead-generation sites (Sulekha), "
        "the unreliability and zero-guarantee of direct directories (Panikkaar), and the high-commission partner fatigue of super-apps (Urban Company).<br/><br/>"
        "<b>Immediate Next Steps for Market Entry:</b><br/>"
        "1. Maintain the 20-min / 60-min tiered acceptance timers and 1-tap auto-reassign flow.<br/>"
        "2. Retain the strict single-PIN doorstep arrival with zero completion OTP fatigue.<br/>"
        "3. Connect the live cloud database (TiDB or Supabase) and deploy the Capacitor mobile shell to Google Play and Apple App Store."
    )
    story.append(Paragraph(conclusion_text, body_style))

    # Build PDF
    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"Successfully generated PDF: {filename}")

if __name__ == '__main__':
    target_path = os.path.join(os.getcwd(), "LAMO_Home_Services_Market_Comparison.pdf")
    build_pdf(target_path)

