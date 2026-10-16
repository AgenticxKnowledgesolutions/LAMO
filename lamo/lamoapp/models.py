import hashlib
import secrets
import string
from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from django.utils import timezone
from datetime import timedelta
from django.urls import reverse


class CustomUserManager(UserManager):
    """Manager that keeps ``role`` in step with Django's superuser flags.

    ``createsuperuser`` (and ``create_superuser()``) only sets ``is_staff`` and
    ``is_superuser``; it knows nothing about ``CustomUser.role``, which would
    otherwise fall back to its ``'USER'`` default. Every access check in this
    project branches on ``role`` - not on ``is_superuser`` - so such an account
    was a real superuser that the app treated as an ordinary customer and sent
    to the customer home page after login.

    A superuser is therefore always given ``role='ADMIN'``. ``is_email_verified``
    is set too: admin logins skip the OTP step, so an unverified flag on an
    admin account only leaves a stale state behind the login form.
    """

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault('role', 'ADMIN')
        extra_fields.setdefault('is_email_verified', True)
        return super().create_superuser(username, email, password, **extra_fields)

    async def acreate_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault('role', 'ADMIN')
        extra_fields.setdefault('is_email_verified', True)
        return await super().acreate_superuser(username, email, password, **extra_fields)


class CustomUser(AbstractUser):

    ROLE_CHOICES = (
        ('ADMIN', 'Admin'),
        ('USER', 'User'),
        ('WORKER', 'Worker'),
    )

    role = models.CharField(
        max_length=10,
        choices=ROLE_CHOICES,
        default='USER'
    )

    # Replaces AbstractUser's stock manager so `createsuperuser` assigns
    # role='ADMIN' (see CustomUserManager). Everything the default manager did
    # - create_user, get_by_natural_key, OTP flows, etc. - is inherited
    # unchanged, so no existing behaviour is altered.
    objects = CustomUserManager()

    is_email_verified = models.BooleanField(default=False)
    email_verification_otp = models.CharField(max_length=6, blank=True, null=True)
    otp_created_at = models.DateTimeField(blank=True, null=True)
    otp_failed_attempts = models.PositiveIntegerField(default=0)
    otp_locked_until = models.DateTimeField(blank=True, null=True)
    password_reset_token = models.CharField(max_length=64, blank=True, null=True)
    # SHA-256 of the reset token. The plaintext token is only ever put in the
    # email link; the database keeps the hash so a DB leak cannot be replayed.
    password_reset_token_hash = models.CharField(max_length=64, blank=True, null=True)
    password_reset_expires = models.DateTimeField(blank=True, null=True)

    def generate_otp(self):
        # secrets (CSPRNG) instead of random - OTPs are an auth credential.
        otp = ''.join(secrets.choice(string.digits) for _ in range(6))
        self.email_verification_otp = otp
        self.otp_created_at = timezone.now()
        self.save(update_fields=['email_verification_otp', 'otp_created_at'])
        return otp

    @staticmethod
    def hash_reset_token(token):
        """Return the SHA-256 hex digest used to store a reset token."""
        return hashlib.sha256(str(token).encode('utf-8')).hexdigest()

    def generate_password_reset_token(self):
        # 48 random bytes -> exactly 64 URL-safe characters.
        token = secrets.token_urlsafe(48)
        # Only the hash is persisted; the plaintext token lives solely in the
        # email link. Any plaintext left over from older rows is still honoured
        # by find_by_reset_token() until it expires.
        self.password_reset_token = None
        self.password_reset_token_hash = self.hash_reset_token(token)
        self.password_reset_expires = timezone.now() + timedelta(hours=1)
        self.save(update_fields=[
            'password_reset_token',
            'password_reset_token_hash',
            'password_reset_expires',
        ])
        return token

    def is_reset_token_valid(self, token):
        if not self.password_reset_expires or not token:
            return False

        if timezone.now() > self.password_reset_expires:
            return False

        # Preferred: compare against the stored hash.
        if self.password_reset_token_hash and secrets.compare_digest(
            self.password_reset_token_hash,
            self.hash_reset_token(token),
        ):
            return True

        # Backwards compatibility: tokens issued before hashing was added.
        if self.password_reset_token and secrets.compare_digest(
            self.password_reset_token,
            str(token),
        ):
            return True

        return False

    @classmethod
    def find_by_reset_token(cls, token):
        """Return the user owning a still-valid reset token, or ``None``.

        Handles both hashed tokens (new) and plaintext tokens (legacy rows),
        so reset links sent before this change keep working until they expire.
        """
        if not token:
            return None

        now = timezone.now()

        user = (
            cls.objects
            .filter(
                password_reset_token_hash=cls.hash_reset_token(token),
                password_reset_expires__gte=now,
            )
            .order_by('-password_reset_expires')
            .first()
        )
        if user:
            return user

        return (
            cls.objects
            .filter(
                password_reset_token=token,
                password_reset_expires__gte=now,
            )
            .order_by('-password_reset_expires')
            .first()
        )

    def is_otp_valid(self, otp):
        if not self.email_verification_otp or not self.otp_created_at:
            return False
        if self.email_verification_otp != otp:
            return False
        expiry = self.otp_created_at + timedelta(minutes=10)
        return timezone.now() <= expiry

    def __str__(self):
        return self.username


class UserProfile(models.Model):
    user = models.OneToOneField(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='user_profile'
    )
    phone = models.CharField(max_length=15, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    district = models.CharField(max_length=50, blank=True, null=True)
    latitude = models.FloatField(blank=True, null=True)
    longitude = models.FloatField(blank=True, null=True)
    profile_image = models.ImageField(upload_to='users/', blank=True, null=True)
    bio = models.TextField(blank=True, null=True)
    date_of_birth = models.DateField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.user.username
    
class Service(models.Model):

    name = models.CharField( max_length=100, unique=True)
    description = models.TextField()
    image = models.ImageField(upload_to='services/', blank=True,  null=True )
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    # icon = models.CharField( max_length=100, default='bi bi-tools')
    is_active = models.BooleanField( default=True)
    is_featured = models.BooleanField( default=False)
    # sort_order = models.PositiveIntegerField( default=0)
    created_at = models.DateTimeField( auto_now_add=True)
    updated_at = models.DateTimeField( auto_now=True )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

# ============================================================
# WORKER PROFILE
# ============================================================

class WorkerProfile(models.Model):

    AVAILABILITY_CHOICES = (
        ('Full Time', 'Full Time'),
        ('Part Time', 'Part Time'),
        ('Weekends', 'Weekends'),
        ('Flexible', 'Flexible'),
    )

    WORK_STATUS_CHOICES = (
        ('AVAILABLE', 'Available'),
        ('ON_DUTY', 'On Duty'),
        ('LEAVE', 'On Leave'),
        ('SUSPENDED', 'Suspended'),
    )

    DISTRICT_CHOICES = (
        ('Thiruvananthapuram', 'Thiruvananthapuram'),
        ('Kollam', 'Kollam'),
        ('Pathanamthitta', 'Pathanamthitta'),
        ('Alappuzha', 'Alappuzha'),
        ('Kottayam', 'Kottayam'),
        ('Idukki', 'Idukki'),
        ('Ernakulam', 'Ernakulam'),
        ('Thrissur', 'Thrissur'),
        ('Palakkad', 'Palakkad'),
        ('Malappuram', 'Malappuram'),
        ('Kozhikode', 'Kozhikode'),
        ('Wayanad', 'Wayanad'),
        ('Kannur', 'Kannur'),
        ('Kasaragod', 'Kasaragod'),
    )

    user = models.OneToOneField(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='worker_profile'
    )

    phone = models.CharField(max_length=15)

    service = models.ForeignKey(
        Service,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='workers'
    )

    experience_years = models.PositiveIntegerField(default=0)

    district = models.CharField(
        max_length=50,
        choices=DISTRICT_CHOICES
    )

    availability = models.CharField(
        max_length=20,
        choices=AVAILABILITY_CHOICES
    )

    work_status = models.CharField(
        max_length=20,
        choices=WORK_STATUS_CHOICES,
        default='AVAILABLE'
    )

    pricing_type = models.CharField(
        max_length=10,
        choices=[
            ('hour', 'Per Hour'),
            ('day', 'Per Day'),
        ],
        default='hour'
    )

    pricing_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0.00
    )

    proof_document = models.FileField(
        upload_to='worker_proofs/'
    )

    PROOF_TYPE_CHOICES = (
        ('aadhaar', 'Aadhaar'),
        ('pan', 'PAN'),
        ('voter_id', 'Voter ID'),
    )

    proof_type = models.CharField(
        max_length=20,
        choices=PROOF_TYPE_CHOICES,
        blank=True,
        null=True,
        help_text="Type of identity proof uploaded"
    )

    custom_service = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Custom service name when 'Other' is selected"
    )

    address = models.TextField()

    current_place = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Auto-detected place name from current location"
    )

    latitude = models.FloatField(
        blank=True,
        null=True,
        help_text="Worker's exact latitude"
    )

    longitude = models.FloatField(
        blank=True,
        null=True,
        help_text="Worker's exact longitude"
    )

    profile_image = models.ImageField(
        upload_to='workers/',
        blank=True,
        null=True
    )

    is_verified = models.BooleanField(default=False)

    strikes_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of confirmed strikes"
    )

    warning_notes = models.TextField(
        blank=True,
        default='',
        help_text="Admin warning and dispute notes"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.user.username
# ============================================================
# BOOKING
# ============================================================

class Booking(models.Model):

    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('ACCEPTED', 'Accepted'),
        ('IN_PROGRESS', 'In Progress'),
        ('COMPLETION_REQUESTED', 'Completion Requested'),
        ('REJECTED', 'Rejected'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
        ('EXPIRED', 'Expired'),
        ('DISPUTED', 'Disputed'),
        ('EMERGENCY_PENDING', 'Emergency Pending'),
    )
    BOOKING_TYPE_CHOICES = (
        ('One Time', 'One Time'),
        ('Recurring', 'Recurring'),
        ('Emergency', 'Emergency'),
    )

    customer = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='bookings'
    )

    worker = models.ForeignKey(
        WorkerProfile,
        on_delete=models.PROTECT,
        related_name='bookings',
        null=True,
        blank=True
    )

    service = models.ForeignKey(
        Service,
        on_delete=models.PROTECT,
        related_name='bookings'
    )

    district = models.CharField(
        max_length=50,
        choices=WorkerProfile.DISTRICT_CHOICES,
        blank=True,
        null=True
    )

    booking_type = models.CharField(
        max_length=20,
        choices=BOOKING_TYPE_CHOICES,
        default='One Time'
    )

    date = models.DateField(
        null=True,
        blank=True
    )

    time = models.CharField(
        max_length=50
    )

    start_time = models.TimeField(
        null=True,
        blank=True
    )

    end_time = models.TimeField(
        null=True,
        blank=True
    )

    location = models.TextField()

    latitude = models.FloatField(
        null=True,
        blank=True
    )

    longitude = models.FloatField(
        null=True,
        blank=True
    )

    description = models.TextField(
        blank=True
    )

    phone = models.CharField(
        max_length=15
    )

    estimated_cost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True
    )

    is_paid = models.BooleanField(
        default=False
    )

    payment_method = models.CharField(
        max_length=50,
        null=True,
        blank=True
    )

    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default='PENDING'
    )

    arrival_otp = models.CharField(
        max_length=6,
        blank=True,
        null=True,
        help_text="6-digit OTP for worker arrival verification"
    )

    arrival_verified_at = models.DateTimeField(
        blank=True,
        null=True
    )

    final_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Final service amount submitted by worker"
    )

    cancellation_reason = models.TextField(
        blank=True,
        default=''
    )

    cancelled_by = models.CharField(
        max_length=30,
        blank=True,
        default=''
    )

    dispute_reason = models.TextField(
        blank=True,
        default=''
    )

    worker_no_show_reported_at = models.DateTimeField(
        blank=True,
        null=True
    )

    customer_no_show_reported_at = models.DateTimeField(
        blank=True,
        null=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    series = models.ForeignKey(
        'BookingSeries',
        on_delete=models.SET_NULL,
        related_name='bookings',
        null=True,
        blank=True,
        help_text="Reference to the recurring series this booking belongs to"
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # Every availability and conflict check filters on
            # (worker, date, status): has_worker_schedule_conflict(),
            # build_worker_availability_maps(), the duplicate-booking guard and
            # the emergency acceptance check. Django only auto-indexes each
            # ForeignKey separately, so worker_id alone cannot narrow by date
            # and status without scanning every booking that worker ever has.
            # Column order follows the query: worker and date are equality
            # filters, status is an IN filter that benefits from coming last.
            models.Index(
                fields=['worker', 'date', 'status'],
                name='booking_worker_date_status_idx',
            ),
        ]

    def generate_arrival_otp(self):
        """Generate and save a 4-digit arrival verification OTP if not already present."""
        if not self.arrival_otp:
            import secrets
            self.arrival_otp = f"{secrets.randbelow(9000) + 1000}"
            if self.pk:
                self.save(update_fields=['arrival_otp'])
        return self.arrival_otp

    def save(self, *args, **kwargs):
        if not self.arrival_otp:
            import secrets
            self.arrival_otp = f"{secrets.randbelow(9000) + 1000}"
        super().save(*args, **kwargs)


    def __str__(self):

        return (
            f"Booking #{self.id} - "
            f"{self.service.name} - "
            f"{self.customer.username}"
        )


class Payment(models.Model):
    PAYMENT_STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('PAID', 'Paid'),
        ('FAILED', 'Failed'),
        ('REFUNDED', 'Refunded'),
    )

    booking = models.OneToOneField(
        Booking,
        on_delete=models.CASCADE,
        related_name='payment'
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0.00
    )
    payment_method = models.CharField(
        max_length=50,
        blank=True,
        null=True
    )
    payment_reference = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        unique=True
    )
    status = models.CharField(
        max_length=20,
        choices=PAYMENT_STATUS_CHOICES,
        default='PENDING'
    )
    gateway = models.CharField(
        max_length=30,
        default='manual'
    )
    paid_at = models.DateTimeField(
        blank=True,
        null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Payment #{self.id} - Booking #{self.booking_id} - {self.status}"


class BookingMessage(models.Model):
    booking = models.ForeignKey(
        Booking,
        on_delete=models.CASCADE,
        related_name='messages'
    )
    sender = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='sent_booking_messages'
    )
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Booking #{self.booking_id} message by {self.sender.username}"


# ============================================================
# BOOKING SERIES (for recurring bookings)
# ============================================================

class BookingSeries(models.Model):
    """Represents a series of recurring bookings"""

    FREQUENCY_CHOICES = (
        ('Daily', 'Daily'),
        ('Weekly', 'Weekly'),
        ('Monthly', 'Monthly'),
    )

    customer = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='booking_series'
    )

    worker = models.ForeignKey(
        WorkerProfile,
        on_delete=models.CASCADE,
        related_name='booking_series'
    )

    service = models.ForeignKey(
        Service,
        on_delete=models.CASCADE,
        related_name='booking_series'
    )

    frequency = models.CharField(
        max_length=20,
        choices=FREQUENCY_CHOICES,
        default='Weekly'
    )

    preferred_day = models.CharField(
        max_length=20,
        blank=True,
        help_text="Preferred day of week for weekly recurrence"
    )

    time = models.CharField(
        max_length=50
    )

    start_time = models.TimeField(
        null=True,
        blank=True
    )

    end_time = models.TimeField(
        null=True,
        blank=True
    )

    location = models.TextField()

    latitude = models.FloatField(
        null=True,
        blank=True
    )

    longitude = models.FloatField(
        null=True,
        blank=True
    )

    description = models.TextField(
        blank=True
    )

    phone = models.CharField(
        max_length=15
    )

    start_date = models.DateField(
        help_text="Start date of the recurring series"
    )

    end_date = models.DateField(
        help_text="End date of the recurring series"
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Whether this series is still active"
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Series #{self.id} - {self.service.name} - {self.customer.username}"


# ============================================================
# NOTIFICATION
# ============================================================

class Notification(models.Model):

    NOTIFICATION_TYPES = (
        ('BOOKING', 'Booking'),
        ('BOOKING_UPDATE', 'Booking Update'),
        ('EMERGENCY', 'Emergency'),
        ('SYSTEM', 'System'),
    )

    recipient = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='notifications'
    )

    booking = models.ForeignKey(
        Booking,
        on_delete=models.CASCADE,
        related_name='notifications',
        null=True,
        blank=True
    )

    notification_type = models.CharField(
        max_length=30,
        choices=NOTIFICATION_TYPES,
        default='SYSTEM'
    )

    message = models.TextField()

    is_read = models.BooleanField(
        default=False
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):

        return f"{self.recipient.username} - {self.message[:40]}"

    def get_target_url(self):
        """Return the appropriate page/context URL for this notification based on role."""
        if not self.recipient:
            return '#'

        role = self.recipient.role

        if role == 'ADMIN':
            if self.booking:
                return reverse('admin_booking_view', args=[self.booking.id])
            if 'worker' in self.message.lower() or 'suspended' in self.message.lower():
                return reverse('workers_list')
            return reverse('admin_dashboard')

        elif role == 'WORKER':
            if self.booking:
                if self.notification_type == 'EMERGENCY':
                    # Worker sees emergency broadcasts on the dashboard
                    return reverse('worker_dashboard')
                if 'new booking message' in self.message.lower() or 'message from' in self.message.lower():
                    return reverse('booking_messages', args=[self.booking.id])
                return f"{reverse('worker_jobs')}#booking-{self.booking.id}"
            return reverse('worker_dashboard')

        else: # USER
            if self.booking:
                if 'new booking message' in self.message.lower() or 'message from' in self.message.lower():
                    return reverse('booking_messages', args=[self.booking.id])
                return f"{reverse('my_bookings')}#booking-{self.booking.id}"
            return reverse('dashboard')

class Complaint(models.Model):

    SEVERITY_CHOICES = (
        ('LOW', 'Low'),
        ('MEDIUM', 'Medium'),
        ('HIGH', 'High'),
    )

    booking = models.ForeignKey(
        Booking,
        on_delete=models.CASCADE,
        related_name='complaints'
    )

    customer = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE
    )

    subject = models.CharField(max_length=200)

    description = models.TextField()

    severity = models.CharField(
        max_length=10,
        choices=SEVERITY_CHOICES,
        default='MEDIUM'
    )

    status = models.CharField(
        max_length=20,
        choices=[
            ('OPEN', 'Open'),
            ('UNDER_REVIEW', 'Under Review'),
            ('RESOLVED', 'Resolved'),
            ('REJECTED', 'Rejected'),
        ],
        default='OPEN'
    )

    admin_remark = models.TextField(
        blank=True,
        null=True
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('booking', 'customer')

    def __str__(self):
        return f"Complaint #{self.id} - Booking #{self.booking_id}"


# ============================================================
# SERVICE REQUEST (Custom "Others" service approval workflow)
# ============================================================

class ServiceRequest(models.Model):

    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
        ('PUBLISHED', 'Published'),
    )

    name = models.CharField(
        max_length=100,
        help_text="Custom service name requested by worker"
    )

    description = models.TextField(
        blank=True,
        help_text="Service description (filled by admin during publish)"
    )

    image = models.ImageField(
        upload_to='services/',
        blank=True,
        null=True,
        help_text="Service image (uploaded by admin during publish)"
    )

    requested_by = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='service_requests',
        help_text="Worker who requested this service"
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='PENDING'
    )

    admin_remark = models.TextField(
        blank=True,
        null=True,
        help_text="Admin remarks for approval/rejection"
    )

    reviewed_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='reviewed_service_requests',
        help_text="Admin who reviewed this request"
    )

    reviewed_at = models.DateTimeField(
        blank=True,
        null=True
    )

    published_service = models.ForeignKey(
        'Service',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='service_request',
        help_text="Linked published service"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} - {self.get_status_display()}"


# ============================================================
# REVIEW
# ============================================================

class Review(models.Model):

    booking = models.OneToOneField(
        Booking,
        on_delete=models.CASCADE,
        related_name='review'
    )

    customer = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='reviews_given'
    )

    worker = models.ForeignKey(
        WorkerProfile,
        on_delete=models.CASCADE,
        related_name='reviews_received'
    )

    rating = models.PositiveIntegerField(
        choices=[
            (1, '1 Star'),
            (2, '2 Stars'),
            (3, '3 Stars'),
            (4, '4 Stars'),
            (5, '5 Stars'),
        ]
    )

    comment = models.TextField(
        blank=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return (
            f"Review - Booking #{self.booking.id} - "
            f"{self.rating} Stars"
        )


# ============================================================
# WORKER LEAVE
# ============================================================

class WorkerLeave(models.Model):
    worker = models.ForeignKey(
        WorkerProfile,
        on_delete=models.CASCADE,
        related_name='leaves'
    )

    leave_date = models.DateField()

    reason = models.CharField(
        max_length=200,
        blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-leave_date']
        unique_together = ['worker', 'leave_date']

    def __str__(self):
        return f"{self.worker.user.username} - {self.leave_date}"


# ============================================================
# CONTACT ENQUIRY
# ============================================================

class ContactEnquiry(models.Model):

    STATUS_CHOICES = (
        ('NEW', 'New'),
        ('READ', 'Read'),
        ('RESOLVED', 'Resolved'),
    )

    name = models.CharField(max_length=100)
    email = models.EmailField()
    phone = models.CharField(max_length=15, blank=True, null=True)
    subject = models.CharField(max_length=200)
    message = models.TextField()

    user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='contact_enquiries',
        help_text="User who submitted the enquiry (if logged in)"
    )

    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default='NEW'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = "Contact Enquiries"

    def __str__(self):
        return f"{self.name} - {self.subject}"


# ============================================================
# STRIKE & NO-SHOW AUDIT RECORD
# ============================================================

class StrikeRecord(models.Model):
    STRIKE_TYPE_CHOICES = (
        ('WORKER_NO_SHOW', 'Worker No Show'),
        ('WORKER_LATE_CANCELLATION', 'Worker Late Cancellation'),
        ('CUSTOMER_NO_SHOW', 'Customer No Show'),
        ('COMPLAINT_UPHELD', 'Complaint Upheld'),
        ('OTHER', 'Other'),
    )

    STATUS_CHOICES = (
        ('PENDING_REVIEW', 'Pending Admin Review'),
        ('CONFIRMED', 'Confirmed Strike'),
        ('DISMISSED', 'Dismissed / Forgiven'),
    )

    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='strike_records',
        help_text="User (worker or customer) receiving the strike/warning report"
    )

    worker = models.ForeignKey(
        WorkerProfile,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='strikes'
    )

    booking = models.ForeignKey(
        Booking,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='strikes'
    )

    strike_type = models.CharField(
        max_length=40,
        choices=STRIKE_TYPE_CHOICES
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='PENDING_REVIEW'
    )

    reason = models.TextField()

    evidence = models.TextField(
        blank=True,
        default='',
        help_text="Evidence such as GPS coordinates, waiting time, or customer notes"
    )

    admin_notes = models.TextField(
        blank=True,
        default='',
        help_text="Admin review findings before applying strike/suspension"
    )

    reviewed_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_strikes'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Strike #{self.id} - {self.user.username} ({self.get_strike_type_display()}) [{self.status}]"