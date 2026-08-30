import random
import string
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
from datetime import timedelta


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

    is_email_verified = models.BooleanField(default=False)
    email_verification_otp = models.CharField(max_length=6, blank=True, null=True)
    otp_created_at = models.DateTimeField(blank=True, null=True)
    password_reset_token = models.CharField(max_length=64, blank=True, null=True)
    password_reset_expires = models.DateTimeField(blank=True, null=True)

    def generate_otp(self):
        otp = ''.join(random.choices(string.digits, k=6))
        self.email_verification_otp = otp
        self.otp_created_at = timezone.now()
        self.save(update_fields=['email_verification_otp', 'otp_created_at'])
        return otp

    def generate_password_reset_token(self):
        token = ''.join(random.choices(string.ascii_letters + string.digits, k=64))
        self.password_reset_token = token
        self.password_reset_expires = timezone.now() + timedelta(hours=1)
        self.save(update_fields=['password_reset_token', 'password_reset_expires'])
        return token

    def is_reset_token_valid(self, token):
        if not self.password_reset_token or not self.password_reset_expires:
            return False
        return (
            self.password_reset_token == token
            and timezone.now() <= self.password_reset_expires
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

    address = models.TextField()

    profile_image = models.ImageField(
        upload_to='workers/',
        blank=True,
        null=True
    )

    is_verified = models.BooleanField(default=False)

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
        related_name='bookings'
    )

    service = models.ForeignKey(
        Service,
        on_delete=models.PROTECT,
        related_name='bookings'
    )

    booking_type = models.CharField(
        max_length=20,
        choices=BOOKING_TYPE_CHOICES,
        default='One Time'
    )

    date = models.DateField()

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
            f"Booking #{self.id} - "
            f"{self.service.name} - "
            f"{self.customer.username}"
        )


# ============================================================
# NOTIFICATION
# ============================================================

class Notification(models.Model):

    NOTIFICATION_TYPES = (
        ('BOOKING', 'Booking'),
        ('BOOKING_UPDATE', 'Booking Update'),
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