from django.contrib import admin
from django.core.exceptions import PermissionDenied
from .models import (
    Booking,
    BookingMessage,
    BookingSeries,
    Complaint,
    ContactEnquiry,
    CustomUser,
    Notification,
    Payment,
    Review,
    Service,
    ServiceRequest,
    WorkerLeave,
    WorkerProfile,
    UserProfile,
    StrikeRecord,
)


@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    list_display = ('username', 'email', 'role', 'is_email_verified', 'is_staff', 'is_active', 'date_joined')
    list_filter = ('role', 'is_email_verified', 'is_staff', 'is_active')
    search_fields = ('username', 'email')
    actions = ['verify_email']

    def _admin_account_guard(self, request, obj):
        """Reject self-sabotage and last-admin removals in Django admin."""
        if obj is None:
            return

        if obj.pk == request.user.pk:
            if obj.role != 'ADMIN' or not obj.is_active or not obj.is_staff:
                raise PermissionDenied(
                    'You cannot delete, suspend, deactivate, or demote your own admin account while logged in.'
                )
            return

        previous = CustomUser.objects.filter(pk=obj.pk).values(
            'role', 'is_active', 'is_staff',
        ).first()
        was_active_admin = previous and (
            previous['role'] == 'ADMIN'
            and previous['is_active']
            and previous['is_staff']
        )
        remains_active_admin = (
            obj.role == 'ADMIN' and obj.is_active and obj.is_staff
        )

        if was_active_admin and not remains_active_admin:
            remaining = CustomUser.objects.filter(
                role='ADMIN',
                is_active=True,
                is_staff=True,
            ).exclude(pk=obj.pk)
            if not remaining.exists():
                raise PermissionDenied(
                    'At least one active admin account must remain available.'
                )

    def save_model(self, request, obj, form, change):
        self._admin_account_guard(request, obj)
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.pk == request.user.pk:
            return False
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset):
        selected_ids = set(queryset.values_list('pk', flat=True))
        if request.user.pk in selected_ids:
            raise PermissionDenied('You cannot delete your own admin account.')
        if queryset.filter(role='ADMIN', is_active=True, is_staff=True).exists():
            remaining = CustomUser.objects.filter(
                role='ADMIN',
                is_active=True,
                is_staff=True,
            ).exclude(pk__in=selected_ids)
            if not remaining.exists():
                raise PermissionDenied(
                    'At least one active admin account must remain available.'
                )
        return super().delete_queryset(request, queryset)

    def get_deleted_objects(self, objs, request):
        selected_ids = {obj.pk for obj in objs}
        if request.user.pk in selected_ids:
            raise PermissionDenied('You cannot delete your own admin account.')
        if any(obj.role == 'ADMIN' for obj in objs):
            remaining = CustomUser.objects.filter(
                role='ADMIN',
                is_active=True,
                is_staff=True,
            ).exclude(pk__in=selected_ids)
            if not remaining.exists():
                raise PermissionDenied(
                    'At least one active admin account must remain available.'
                )
        return super().get_deleted_objects(objs, request)

    @admin.action(description='Mark selected users as email verified')
    def verify_email(self, request, queryset):
        queryset.update(is_email_verified=True)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'phone', 'district', 'updated_at')
    search_fields = ('user__username', 'phone', 'district')
    list_filter = ('district',)


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ('name', 'price', 'is_active', 'is_featured', 'created_at')
    list_filter = ('is_active', 'is_featured')
    search_fields = ('name', 'description')


@admin.register(ServiceRequest)
class ServiceRequestAdmin(admin.ModelAdmin):
    list_display = ('name', 'requested_by', 'status', 'created_at', 'updated_at')
    list_filter = ('status', 'created_at')
    search_fields = ('name', 'requested_by__username', 'admin_remark')
    readonly_fields = ('created_at', 'updated_at', 'published_service')


@admin.register(WorkerProfile)
class WorkerProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'service', 'phone', 'district', 'pricing_type', 'pricing_amount', 'work_status', 'is_verified')
    list_filter = ('work_status', 'is_verified', 'district', 'service', 'pricing_type')
    search_fields = ('user__username', 'phone', 'district')


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ('id', 'customer', 'worker', 'service', 'status', 'date', 'estimated_cost', 'created_at')
    list_filter = ('status', 'booking_type', 'date')
    search_fields = ('customer__username', 'worker__user__username', 'service__name')


@admin.register(BookingMessage)
class BookingMessageAdmin(admin.ModelAdmin):
    list_display = ('booking', 'sender', 'message', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('booking__id', 'sender__username', 'message')


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('recipient', 'notification_type', 'message', 'is_read', 'created_at')
    list_filter = ('notification_type', 'is_read')


@admin.register(Complaint)
class ComplaintAdmin(admin.ModelAdmin):
    list_display = ('id', 'customer', 'booking', 'subject', 'severity', 'status', 'created_at')
    list_filter = ('status', 'severity')
    search_fields = ('subject', 'description', 'customer__username')


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ('id', 'customer', 'worker', 'rating', 'created_at')
    list_filter = ('rating',)


@admin.register(WorkerLeave)
class WorkerLeaveAdmin(admin.ModelAdmin):
    list_display = ('worker', 'leave_date', 'reason', 'created_at')
    list_filter = ('leave_date',)
    search_fields = ('worker__user__username', 'reason')


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'booking',
        'amount',
        'payment_method',
        'gateway',
        'status',
        'paid_at',
        'created_at',
    )
    list_filter = ('status', 'gateway', 'payment_method')
    search_fields = (
        'booking__id',
        'payment_reference',
        'booking__customer__username',
    )
    readonly_fields = ('created_at', 'updated_at')


@admin.register(BookingSeries)
class BookingSeriesAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'service',
        'customer',
        'worker',
        'frequency',
        'start_date',
        'end_date',
        'is_active',
    )
    list_filter = ('frequency', 'is_active', 'start_date')
    search_fields = (
        'customer__username',
        'worker__user__username',
        'service__name',
    )
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ContactEnquiry)
class ContactEnquiryAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'email', 'subject', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('name', 'email', 'subject', 'message')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(StrikeRecord)
class StrikeRecordAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'strike_type', 'status', 'booking', 'created_at')
    list_filter = ('strike_type', 'status', 'created_at')
    search_fields = ('user__username', 'user__email', 'reason', 'evidence', 'admin_notes')
    readonly_fields = ('created_at', 'updated_at')
