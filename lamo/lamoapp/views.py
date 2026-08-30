import re
import random
import string

from django.db import transaction
from django.db.models import Q, Sum, Avg, Count, Case, When, Value, IntegerField
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.views.decorators.cache import never_cache
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from .models import CustomUser, UserProfile, Service, WorkerProfile, Booking, Notification, Complaint, Review, WorkerLeave
from .forms import ServiceForm, UserProfileForm, WorkerProfileForm
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.conf import settings
from datetime import datetime, time as datetime_time

def parse_time_range(time_str):
    if not time_str:
        return None, None
    time_str = str(time_str).strip()
    parts = re.split(r'\s*(?:-|to|until)\s*', time_str, flags=re.IGNORECASE)

    def parse_single_time(t_str):
        t_str = t_str.strip().upper()
        formats = [
            "%I:%M %p", "%I %p", "%I:%M%p", "%I%p",
            "%H:%M", "%H:%M:%S"
        ]
        for fmt in formats:
            try:
                return datetime.strptime(t_str, fmt).time()
            except ValueError:
                continue
        return None

    if len(parts) >= 2:
        start_t = parse_single_time(parts[0])
        end_t = parse_single_time(parts[1])
        if start_t and end_t:
            return start_t, end_t

    if len(parts) == 1:
        start_t = parse_single_time(parts[0])
        if start_t:
            end_h = (start_t.hour + 1) % 24
            return start_t, datetime_time(end_h, start_t.minute, start_t.second)

    return None, None

def home(request):

    query = request.GET.get('q', '').strip()
    services = Service.objects.filter(is_active=True)
    if query:
        services = services.filter(
            Q(name__icontains=query) | Q(description__icontains=query)
        )
    services = services.order_by('-created_at')

    return render(request, 'home.html', {
        'services': services,
        'query': query,
    })


def send_otp_email(user):
    otp = user.generate_otp()
    subject = 'LAMO - Your Email Verification OTP'
    message = (
        f'Hi {user.username},\n\n'
        f'Your OTP for email verification is: {otp}\n\n'
        f'This OTP is valid for 10 minutes.\n'
        f'Do not share this OTP with anyone.\n\n'
        f'Thanks,\nLAMO Team'
    )
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
    except Exception:
        print(f"\n{'='*50}")
        print(f"  OTP EMAIL (SMTP not configured)")
        print(f"  To: {user.email}")
        print(f"  OTP: {otp}")
        print(f"{'='*50}\n")
    return otp


def verify_otp_view(request):
    if request.user.is_authenticated and request.user.is_email_verified:
        return redirect('home')

    if request.method == 'POST':
        action = request.POST.get('action', 'verify')
        otp_input = request.POST.get('otp', '').strip()

        if action == 'verify':
            user_id = request.session.get('otp_user_id')
            if not user_id:
                messages.error(request, 'Session expired. Please register again.')
                return redirect('register')

            try:
                user = CustomUser.objects.get(id=user_id)
            except CustomUser.DoesNotExist:
                messages.error(request, 'User not found. Please register again.')
                return redirect('register')

            if user.is_otp_valid(otp_input):
                user.is_email_verified = True
                user.email_verification_otp = None
                user.otp_created_at = None
                user.save(update_fields=['is_email_verified', 'email_verification_otp', 'otp_created_at'])

                if user.role == 'USER':
                    UserProfile.objects.get_or_create(user=user)

                messages.success(request, 'Email verified successfully! You can now login.')
                del request.session['otp_user_id']
                return redirect('login')
            else:
                messages.error(request, 'Invalid or expired OTP. Please try again.')
                return render(request, 'accounts/verify_otp.html', {'user_email': user.email})

        elif action == 'resend':
            user_id = request.session.get('otp_user_id')
            if not user_id:
                messages.error(request, 'Session expired. Please register again.')
                return redirect('register')
            try:
                user = CustomUser.objects.get(id=user_id)
            except CustomUser.DoesNotExist:
                messages.error(request, 'User not found. Please register again.')
                return redirect('register')

            send_otp_email(user)
            messages.success(request, 'A new OTP has been sent to your email.')
            return render(request, 'accounts/verify_otp.html', {'user_email': user.email})

    user_id = request.session.get('otp_user_id')
    user_email = None
    if user_id:
        try:
            user = CustomUser.objects.get(id=user_id)
            user_email = user.email
        except CustomUser.DoesNotExist:
            pass

    return render(request, 'accounts/verify_otp.html', {'user_email': user_email})


# def register_view(request):

#     if request.user.is_authenticated:
#         return redirect('dashboard')

#     if request.method == 'POST':

#         username = request.POST.get('username')
#         email = request.POST.get('email')
#         password = request.POST.get('password')
#         confirm_password = request.POST.get('confirm_password')
#         role = request.POST.get('role')

#         # Password validation
#         if password != confirm_password:
#             messages.error(request, 'Passwords do not match.')
#             return redirect('register')

#         # Username validation
#         if CustomUser.objects.filter(username=username).exists():
#             messages.error(request, 'Username already exists.')
#             return redirect('register')

#         # Email validation
#         if CustomUser.objects.filter(email=email).exists():
#             messages.error(request, 'Email already exists.')
#             return redirect('register')

#         # Create user
#         user = CustomUser.objects.create_user(
#             username=username,
#             email=email,
#             password=password,
#             role=role
#         )

#         messages.success(
#             request,
#             'Account created successfully. Please login.'
#         )

#         return redirect('login')

#     return render(request, 'accounts/register.html')

def about(request):
    return render(request, 'about.html')


def contact(request):
    return render(request, 'contact.html')


def book_service(request):
    if request.user.role != 'USER':
        messages.error(request, 'Only users can book services.')
        return redirect('dashboard')

    if request.method == 'GET':
        worker_id = request.GET.get('worker')
        service_id = request.GET.get('service')
        district = request.GET.get('district', '').strip()

        worker = None
        service = None

        if worker_id:
            worker = get_object_or_404(
                WorkerProfile.objects.select_related('user', 'service'),
                id=worker_id,
                is_verified=True
            )

        if service_id:
            service = get_object_or_404(Service, id=service_id, is_active=True)

        if worker and service:
            if worker.service_id != service.id:
                messages.error(request, 'Selected worker does not provide this service.')
                return redirect('service_detail', service.id)

        return render(request, 'user/book_service.html', {
            'worker': worker,
            'service': service,
            'district': district,
        })

    if request.method == 'POST':
        worker_id = request.POST.get('worker')
        service_id = request.POST.get('service')
        booking_type = request.POST.get('booking_type', 'One Time')
        date = request.POST.get('date')
        time = request.POST.get('time')
        location = request.POST.get('location', '').strip()
        latitude = request.POST.get('latitude', '').strip()
        longitude = request.POST.get('longitude', '').strip()
        description = request.POST.get('description', '').strip()
        phone = request.POST.get('phone', '').strip()

        form_data = {
            'booking_type': booking_type,
            'date': date,
            'time': time,
            'location': location,
            'latitude': latitude,
            'longitude': longitude,
            'description': description,
            'phone': phone,
        }

        worker = get_object_or_404(
            WorkerProfile.objects.select_related('user', 'service'),
            id=worker_id,
            is_verified=True
        )

        service = get_object_or_404(Service, id=service_id, is_active=True)

        if worker.service_id != service.id:
            messages.error(request, 'This worker is not assigned to the selected service.')
            return redirect('service_detail', service.id)

        other_workers = WorkerProfile.objects.filter(
            service=service, is_verified=True, work_status='AVAILABLE'
        ).exclude(id=worker.id).select_related('user')[:4]

        is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'

        def respond_error(err_msg):
            if is_ajax:
                return JsonResponse({'success': False, 'error_message': err_msg}, status=400)
            return render(request, 'user/book_service.html', {
                'worker': worker,
                'service': service,
                'error_message': err_msg,
                'form_data': form_data,
                'other_workers': list(other_workers),
            })

        if not date:
            return respond_error('Please select a date.')
        if not time:
            return respond_error('Please select a time.')
        if not location:
            return respond_error('Please enter the service location.')
        if not phone:
            return respond_error('Please enter your phone number.')

        if worker.work_status != 'AVAILABLE':
            return respond_error('This worker is currently unavailable. Please choose another worker.')

        from datetime import datetime, date as date_type
        try:
            booking_date = datetime.strptime(date, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return respond_error('Invalid date format.')

        if booking_date < date_type.today():
            return respond_error('Cannot book for past dates.')

        worker_on_leave = WorkerLeave.objects.filter(
            worker=worker, leave_date=booking_date
        ).exists()

        if worker_on_leave:
            return respond_error('This worker is on leave on the selected date. Please choose another date.')

        duplicate_user_booking = Booking.objects.filter(
            customer=request.user, worker=worker, service=service, date=date,
            status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS']
        ).exists()

        if duplicate_user_booking:
            return respond_error('You already have an active booking with this worker for the same service and date.')

        start_t, end_t = parse_time_range(time)

        with transaction.atomic():
            locked_worker = WorkerProfile.objects.select_for_update().get(id=worker.id)

            if start_t and end_t:
                existing_booking = Booking.objects.filter(
                    worker=locked_worker, date=date,
                    status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED'],
                    start_time__lt=end_t, end_time__gt=start_t
                ).exists()
            else:
                existing_booking = Booking.objects.filter(
                    worker=locked_worker, date=date, time=time,
                    status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED']
                ).exists()

            if existing_booking:
                return respond_error('This worker is already booked for the selected time.')

            booking = Booking.objects.create(
                customer=request.user, worker=locked_worker, service=service,
                booking_type=booking_type, date=date, time=time,
                start_time=start_t, end_time=end_t,
                location=location, description=description, phone=phone,
                latitude=latitude if latitude else None,
                longitude=longitude if longitude else None,
                estimated_cost=service.price, status='PENDING'
            )

            Notification.objects.create(
                recipient=worker.user, booking=booking, notification_type='BOOKING',
                message=f'New booking request from {request.user.get_full_name() or request.user.username} for {service.name} on {date}.'
            )

            admins = CustomUser.objects.filter(role='ADMIN', is_active=True)
            for admin in admins:
                Notification.objects.create(
                    recipient=admin, booking=booking, notification_type='BOOKING',
                    message=f'New booking #{booking.id}: {service.name} booked by {request.user.get_full_name() or request.user.username}.'
                )

        try:
            send_mail(
                subject=f'LAMO - Booking #{booking.id} Confirmed',
                message=(
                    f'Hi {request.user.username},\n\nYour booking has been confirmed!\n\n'
                    f'- Booking ID: #{booking.id}\n- Service: {service.name}\n'
                    f'- Worker: {worker.user.get_full_name() or worker.user.username}\n'
                    f'- Date: {date}\n- Time: {time}\n\nThanks for choosing LAMO!'
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[request.user.email],
                fail_silently=True,
            )
        except Exception:
            pass

        messages.success(request, 'Booking submitted successfully!')
        return redirect('my_bookings')


@login_required
def worker_accept_booking(request, booking_id):
    if request.user.role != 'WORKER':
        messages.error(request, 'You are not authorized.')
        return redirect('dashboard')

    worker = get_object_or_404(WorkerProfile, user=request.user)

    with transaction.atomic():
        locked_worker = WorkerProfile.objects.select_for_update().get(id=worker.id)
        booking = get_object_or_404(
            Booking.objects.select_related('customer', 'worker__user', 'service'),
            id=booking_id, worker=locked_worker
        )

        if booking.status != 'PENDING':
            messages.warning(request, 'This booking has already been processed.')
            return redirect('worker_dashboard')

        if booking.start_time and booking.end_time:
            has_conflict = Booking.objects.filter(
                worker=locked_worker, date=booking.date,
                status__in=['ACCEPTED', 'IN_PROGRESS'],
                start_time__lt=booking.end_time, end_time__gt=booking.start_time
            ).exclude(id=booking.id).exists()
        else:
            has_conflict = Booking.objects.filter(
                worker=locked_worker, date=booking.date, time=booking.time,
                status__in=['ACCEPTED', 'IN_PROGRESS']
            ).exclude(id=booking.id).exists()

        if has_conflict:
            messages.error(request, 'Cannot accept - overlaps with another booking.')
            return redirect('worker_dashboard')

        booking.status = 'ACCEPTED'
        booking.save(update_fields=['status', 'updated_at'])

    Notification.objects.create(
        recipient=booking.customer, booking=booking, notification_type='BOOKING_UPDATE',
        message=f'Good news! Your {booking.service.name} booking has been accepted by {request.user.get_full_name() or request.user.username}.'
    )

    try:
        send_mail(
            subject=f'LAMO - Booking #{booking.id} Accepted',
            message=(
                f'Hi {booking.customer.username},\n\n'
                f'Your {booking.service.name} booking has been accepted!\n'
                f'- Worker: {request.user.get_full_name() or request.user.username}\n'
                f'- Date: {booking.date}\n\nThanks for choosing LAMO!'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[booking.customer.email],
            fail_silently=True,
        )
    except Exception:
        pass

    admins = CustomUser.objects.filter(role='ADMIN', is_active=True)
    for admin in admins:
        Notification.objects.create(
            recipient=admin, booking=booking, notification_type='BOOKING_UPDATE',
            message=f'Booking #{booking.id} has been accepted by {request.user.get_full_name() or request.user.username}.'
        )

    messages.success(request, 'Booking accepted successfully.')
    return redirect('worker_dashboard')


@login_required
def worker_reject_booking(request, booking_id):
    if request.user.role != 'WORKER':
        messages.error(request, 'You are not authorized.')
        return redirect('dashboard')

    worker = get_object_or_404(WorkerProfile, user=request.user)
    booking = get_object_or_404(Booking, id=booking_id, worker=worker)

    if booking.status != 'PENDING':
        messages.warning(request, 'This booking has already been processed.')
        return redirect('worker_dashboard')

    booking.status = 'REJECTED'
    booking.save(update_fields=['status', 'updated_at'])

    Notification.objects.create(
        recipient=booking.customer, booking=booking, notification_type='BOOKING_UPDATE',
        message=f'Your {booking.service.name} booking has been declined by the worker.'
    )

    try:
        send_mail(
            subject=f'LAMO - Booking #{booking.id} Update',
            message=(
                f'Hi {booking.customer.username},\n\n'
                f'Unfortunately, your {booking.service.name} booking on {booking.date} '
                f'has been declined by the worker.\n\n'
                f'You can book another worker from our platform.\n\n'
                f'Thanks,\nLAMO Team'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[booking.customer.email],
            fail_silently=True,
        )
    except Exception:
        pass

    messages.success(request, 'Booking rejected.')
    return redirect('worker_dashboard')


@login_required
def service_create(request):
    if not request.user.is_staff:
        return redirect('dashboard')

    if request.method == 'POST':
        form = ServiceForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, 'Service created successfully.')
            return redirect('service_list')
    else:
        form = ServiceForm()

    return render(request, 'lamo_service/service_form.html', {
        'form': form, 'title': 'Add Service'
    })


def register_view(request):

    if request.user.is_authenticated:
        return redirect('dashboard')

    services = Service.objects.filter(
        is_active=True
    ).order_by('-created_at')

    if request.method == 'POST':

        username = request.POST.get(
            'username',
            ''
        ).strip()

        email = request.POST.get(
            'email',
            ''
        ).strip()

        password = request.POST.get(
            'password',
            ''
        )

        confirm_password = request.POST.get(
            'confirm_password',
            ''
        )

        role = request.POST.get(
            'role',
            ''
        ).strip()

        # ==================================================
        # BASIC VALIDATION
        # ==================================================

        if not username:

            messages.error(
                request,
                'Username is required.'
            )

            return render(
                request,
                'accounts/register.html',
                {'services': services}
            )

        if not email:

            messages.error(
                request,
                'Email is required.'
            )

            return render(
                request,
                'accounts/register.html',
                {'services': services}
            )

        # EMAIL FORMAT

        try:

            validate_email(email)

        except ValidationError:

            messages.error(
                request,
                'Please enter a valid email address.'
            )

            return render(
                request,
                'accounts/register.html',
                {'services': services}
            )

        # PASSWORD

        if password != confirm_password:

            messages.error(
                request,
                'Passwords do not match.'
            )

            return render(
                request,
                'accounts/register.html',
                {'services': services}
            )

        if len(password) < 8:

            messages.error(
                request,
                'Password must contain at least 8 characters.'
            )

            return render(
                request,
                'accounts/register.html',
                {'services': services}
            )

        # ROLE

        if role not in [
            'USER',
            'WORKER'
        ]:

            messages.error(
                request,
                'Please select User or Worker.'
            )

            return render(
                request,
                'accounts/register.html',
                {'services': services}
            )

        # ==================================================
        # USERNAME VALIDATION
        # ==================================================

        if CustomUser.objects.filter(
            username=username
        ).exists():

            messages.error(
                request,
                'Username already exists.'
            )

            return render(
                request,
                'accounts/register.html',
                {'services': services}
            )

        # ==================================================
        # EMAIL VALIDATION
        # ==================================================

        if CustomUser.objects.filter(
            email__iexact=email
        ).exists():

            messages.error(
                request,
                'Email already exists.'
            )

            return render(
                request,
                'accounts/register.html',
                {'services': services}
            )

        # ==================================================
        # WORKER VALIDATION
        # ==================================================

        worker_data = {}

        if role == 'WORKER':

            # ------------------------------------------------
            # GET WORKER DATA
            # ------------------------------------------------

            phone = request.POST.get(
                'phone',
                ''
            ).strip()

            service_id = request.POST.get(
                'service',
                ''
            ).strip()

            experience = request.POST.get(
                'experience_years',
                ''
            ).strip()

            district = request.POST.get(
                'district',
                ''
            ).strip()

            availability = request.POST.get(
                'availability',
                ''
            ).strip()

            pricing_type = request.POST.get(
                'pricing_type',
                'hour'
            ).strip()

            pricing_amount = request.POST.get(
                'pricing_amount',
                ''
            ).strip()

            address = request.POST.get(
                'address',
                ''
            ).strip()

            profile_image = request.FILES.get(
                'profile_image'
            )

            proof_document = request.FILES.get(
                'proof_document'
            )

            # ------------------------------------------------
            # PHONE VALIDATION
            # ------------------------------------------------

            if not re.fullmatch(
                r'[6-9]\d{9}',
                phone
            ):

                messages.error(
                    request,
                    'Please enter a valid 10-digit Indian mobile number.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ------------------------------------------------
            # DUPLICATE PHONE
            # ------------------------------------------------

            if WorkerProfile.objects.filter(
                phone=phone
            ).exists():

                messages.error(
                    request,
                    'This phone number is already registered.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ------------------------------------------------
            # SERVICE
            # ------------------------------------------------

            if not service_id:

                messages.error(
                    request,
                    'Please select a service.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            try:

                service = Service.objects.get(
                    id=service_id,
                    is_active=True
                )

            except (
                Service.DoesNotExist,
                ValueError
            ):

                messages.error(
                    request,
                    'Please select a valid service.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ------------------------------------------------
            # EXPERIENCE
            # ------------------------------------------------

            if experience == '':

                messages.error(
                    request,
                    'Please enter your experience.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            try:

                experience = int(experience)

            except (
                ValueError,
                TypeError
            ):

                messages.error(
                    request,
                    'Please enter a valid experience.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            if experience < 0:

                messages.error(
                    request,
                    'Experience cannot be negative.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            if experience > 60:

                messages.error(
                    request,
                    'Experience cannot exceed 60 years.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ------------------------------------------------
            # DISTRICT
            # ------------------------------------------------

            valid_districts = [
                'Thiruvananthapuram',
                'Kollam',
                'Pathanamthitta',
                'Alappuzha',
                'Kottayam',
                'Idukki',
                'Ernakulam',
                'Thrissur',
                'Palakkad',
                'Malappuram',
                'Kozhikode',
                'Wayanad',
                'Kannur',
                'Kasaragod',
            ]

            if district not in valid_districts:

                messages.error(
                    request,
                    'Please select a valid district.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ------------------------------------------------
            # AVAILABILITY
            # ------------------------------------------------

            valid_availability = [
                'Full Time',
                'Part Time',
                'Weekends',
                'Flexible',
            ]

            if availability not in valid_availability:

                messages.error(
                    request,
                    'Please select a valid availability.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ------------------------------------------------
            # PRICING VALIDATION
            # ------------------------------------------------

            if pricing_type not in ['hour', 'day']:

                messages.error(
                    request,
                    'Please select a valid pricing type.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            if pricing_amount == '':

                messages.error(
                    request,
                    'Please enter your pricing amount.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            try:

                pricing_amount = float(pricing_amount)

            except (
                ValueError,
                TypeError
            ):

                messages.error(
                    request,
                    'Please enter a valid pricing amount.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            if pricing_amount < 0:

                messages.error(
                    request,
                    'Pricing amount cannot be negative.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            if pricing_amount > 100000:

                messages.error(
                    request,
                    'Pricing amount seems too high. Please enter a valid amount.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ------------------------------------------------
            # ADDRESS
            # ------------------------------------------------

            if not address:

                messages.error(
                    request,
                    'Please enter your address.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            if len(address) < 10:

                messages.error(
                    request,
                    'Please enter a complete address.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ------------------------------------------------
            # PROOF DOCUMENT
            # ------------------------------------------------

            if not proof_document:

                messages.error(
                    request,
                    'Please upload your proof document.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            allowed_extensions = [
                '.pdf',
                '.jpg',
                '.jpeg',
                '.png',
            ]

            file_name = (
                proof_document.name.lower()
            )

            if not any(
                file_name.endswith(ext)
                for ext in allowed_extensions
            ):

                messages.error(
                    request,
                    'Proof must be a PDF, JPG, JPEG or PNG file.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # 5 MB MAXIMUM

            if proof_document.size > 5 * 1024 * 1024:

                messages.error(
                    request,
                    'Proof document must be less than 5 MB.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ------------------------------------------------
            # PROFILE IMAGE
            # ------------------------------------------------

            if profile_image:

                allowed_image_extensions = [
                    '.jpg',
                    '.jpeg',
                    '.png',
                    '.webp',
                ]

                image_name = (
                    profile_image.name.lower()
                )

                if not any(
                    image_name.endswith(ext)
                    for ext in allowed_image_extensions
                ):

                    messages.error(
                        request,
                        'Profile image must be JPG, JPEG, PNG or WEBP.'
                    )

                    return render(
                        request,
                        'accounts/register.html',
                        {'services': services}
                    )

                if profile_image.size > 5 * 1024 * 1024:

                    messages.error(
                        request,
                        'Profile image must be less than 5 MB.'
                    )

                    return render(
                        request,
                        'accounts/register.html',
                        {'services': services}
                    )

            # ------------------------------------------------
            # WORKER DATA
            # ------------------------------------------------

            worker_data = {

                'phone': phone,

                'service': service,

                'experience_years': experience,

                'district': district,

                'availability': availability,

                'pricing_type': pricing_type,

                'pricing_amount': pricing_amount,

                'address': address,

                'proof_document': proof_document,

                'profile_image': profile_image,

            }

        # ==================================================
        # CREATE ACCOUNT
        # ==================================================

        try:

            with transaction.atomic():

                user = CustomUser.objects.create_user(
                    username=username,
                    email=email,
                    password=password,
                    role=role
                )

                # --------------------------------------------
                # WORKER PROFILE
                # --------------------------------------------

                if role == 'WORKER':

                    WorkerProfile.objects.create(
                        user=user,
                        phone=worker_data['phone'],
                        service=worker_data['service'],
                        experience_years=worker_data[
                            'experience_years'
                        ],
                        district=worker_data['district'],
                        availability=worker_data[
                            'availability'
                        ],
                        pricing_type=worker_data[
                            'pricing_type'
                        ],
                        pricing_amount=worker_data[
                            'pricing_amount'
                        ],
                        address=worker_data['address'],
                        proof_document=worker_data[
                            'proof_document'
                        ],
                        profile_image=worker_data[
                            'profile_image'
                        ],
                        is_verified=False
                    )

        except Exception:

            messages.error(
                request,
                'Could not create the account. Please try again.'
            )

            return render(
                request,
                'accounts/register.html',
                {'services': services}
            )

        # ==================================================
        # SUCCESS — SEND OTP FOR EMAIL VERIFICATION
        # ==================================================

        send_otp_email(user)
        request.session['otp_user_id'] = user.id

        messages.success(
            request,
            'Account created successfully. Please verify your email with the OTP sent.'
        )
        return redirect('verify_otp')

    return render(
        request,
        'accounts/register.html',
        {
            'services': services
        }
    )

@never_cache
def forgot_password_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        if not email:
            messages.error(request, 'Please enter your email address.')
            return render(request, 'accounts/forgot_password.html')

        try:
            user = CustomUser.objects.get(email__iexact=email)
        except CustomUser.DoesNotExist:
            messages.success(request, 'If an account with that email exists, a password reset link has been sent.')
            return render(request, 'accounts/forgot_password.html')

        token = user.generate_password_reset_token()
        reset_url = f"http://127.0.0.1:8000/reset-password/{token}/"

        send_mail(
            subject='LAMO - Password Reset Request',
            message=(
                f'Hi {user.username},\n\n'
                f'You requested a password reset. Click the link below to reset your password:\n\n'
                f'{reset_url}\n\n'
                f'This link expires in 1 hour.\n'
                f'If you did not request this, please ignore this email.\n\n'
                f'Thanks,\nLAMO Team'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=True,
        )

        messages.success(request, 'If an account with that email exists, a password reset link has been sent.')
        return render(request, 'accounts/forgot_password.html')

    return render(request, 'accounts/forgot_password.html')


@never_cache
def reset_password_view(request, token):
    if request.user.is_authenticated:
        return redirect('dashboard')

    try:
        user = CustomUser.objects.get(password_reset_token=token)
    except CustomUser.DoesNotExist:
        messages.error(request, 'Invalid or expired reset link.')
        return redirect('login')

    if not user.is_reset_token_valid(token):
        messages.error(request, 'This reset link has expired. Please request a new one.')
        return redirect('forgot_password')

    if request.method == 'POST':
        password = request.POST.get('password', '').strip()
        confirm_password = request.POST.get('confirm_password', '').strip()

        if len(password) < 8:
            messages.error(request, 'Password must be at least 8 characters.')
            return render(request, 'accounts/reset_password.html', {'token': token})

        if password != confirm_password:
            messages.error(request, 'Passwords do not match.')
            return render(request, 'accounts/reset_password.html', {'token': token})

        user.set_password(password)
        user.password_reset_token = None
        user.password_reset_expires = None
        user.save(update_fields=['password', 'password_reset_token', 'password_reset_expires'])

        messages.success(request, 'Password reset successfully. Please login with your new password.')
        return redirect('login')

    return render(request, 'accounts/reset_password.html', {'token': token})

@never_cache
def login_view(request):

    # If already logged in
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':

        username = request.POST.get(
            'username',
            ''
        ).strip()

        password = request.POST.get(
            'password',
            ''
        )

        user = authenticate(
            request,
            username=username,
            password=password
        )

        if user is not None:

            # ==================================================
            # EMAIL VERIFICATION CHECK (User & Worker only)
            # ==================================================
            if user.role in ['USER', 'WORKER'] and not user.is_email_verified:
                send_otp_email(user)
                request.session['otp_user_id'] = user.id
                messages.warning(
                    request,
                    'Please verify your email before logging in. '
                    'An OTP has been sent to your email.'
                )
                return redirect('verify_otp')

            # ==================================================
            # WORKER VERIFICATION CHECK
            # ==================================================

            if user.role == 'WORKER':

                try:
                    worker_profile = user.worker_profile

                except WorkerProfile.DoesNotExist:

                    messages.error(
                        request,
                        'Worker profile not found.'
                    )

                    return redirect('login')


                # Worker is NOT verified
                if not worker_profile.is_verified:

                    messages.warning(
                        request,
                        'Your worker account is waiting for admin verification. '
                        'You cannot login until your account is verified.'
                    )

                    return redirect('login')


            # ==================================================
            # LOGIN
            # ==================================================

            login(
                request,
                user
            )


            # ==================================================
            # ROLE BASED REDIRECTION
            # ==================================================

            if user.role == 'ADMIN':

                return redirect(
                    'admin_dashboard'
                )


            elif user.role == 'WORKER':

                return redirect(
                    'worker_dashboard'
                )


            elif user.role == 'USER':

                return redirect(
                    'home'
                )


            # ==================================================
            # UNKNOWN ROLE
            # ==================================================

            logout(request)

            messages.error(
                request,
                'Invalid user role.'
            )

            return redirect('login')


        else:

            messages.error(
                request,
                'Invalid username or password.'
            )


    return render(
        request,
        'accounts/login.html'
    )
def logout_view(request):

    logout(request)

    return redirect('home')


def dashboard(request):

    if not request.user.is_authenticated:
        return redirect('login')

    if request.user.role == 'ADMIN':
        return redirect('admin_dashboard')

    elif request.user.role == 'USER':
        return redirect('home')

    elif request.user.role == 'WORKER':
        return redirect('worker_dashboard')

    return redirect('home')

@never_cache
def admin_dashboard(request):

    if not request.user.is_authenticated:
        return redirect('login')

    if request.user.role != 'ADMIN':
        return redirect('dashboard')

    from django.db.models import Count, Avg

    services = Service.objects.all()
    users = CustomUser.objects.filter(role='USER')
    workers = CustomUser.objects.filter(role='WORKER')

    total_bookings = Booking.objects.count()
    pending_bookings = Booking.objects.filter(status='PENDING').count()
    completed_bookings = Booking.objects.filter(status='COMPLETED').count()
    active_bookings = Booking.objects.filter(status__in=['ACCEPTED', 'IN_PROGRESS']).count()

    total_complaints = Complaint.objects.count()
    open_complaints = Complaint.objects.filter(status='OPEN').count()

    total_revenue = Booking.objects.filter(is_paid=True).aggregate(total=Sum('estimated_cost'))['total'] or 0
    avg_rating = Review.objects.all().aggregate(avg=Avg('rating'))['avg'] or 0

    recent_bookings = Booking.objects.select_related('customer', 'worker__user', 'service').order_by('-created_at')[:10]

    booking_status_data = {
        'Pending': Booking.objects.filter(status='PENDING').count(),
        'Accepted': Booking.objects.filter(status='ACCEPTED').count(),
        'In Progress': Booking.objects.filter(status='IN_PROGRESS').count(),
        'Completed': Booking.objects.filter(status='COMPLETED').count(),
        'Cancelled': Booking.objects.filter(status='CANCELLED').count(),
    }

    top_workers = WorkerProfile.objects.annotate(
        total_jobs=Count('bookings'),
        avg_rating=Avg('reviews_received__rating')
    ).order_by('-total_jobs')[:5]

    return render(
        request,
        'dashboards/admin_dashboard.html',
        {
            'services': services,
            'users': users,
            'workers': workers,
            'total_bookings': total_bookings,
            'pending_bookings': pending_bookings,
            'completed_bookings': completed_bookings,
            'active_bookings': active_bookings,
            'total_complaints': total_complaints,
            'open_complaints': open_complaints,
            'total_revenue': total_revenue,
            'avg_rating': round(avg_rating, 1),
            'recent_bookings': recent_bookings,
            'booking_status_data': booking_status_data,
            'top_workers': top_workers,
        }
    )


@never_cache
def worker_dashboard(request):

    if not request.user.is_authenticated:
        return redirect('login')

    if request.user.role != 'WORKER':
        return redirect('dashboard')

    try:
        worker_profile = request.user.worker_profile
    except WorkerProfile.DoesNotExist:
        messages.error(request, 'Worker profile not found.')
        return redirect('dashboard')

    pending_bookings = Booking.objects.filter(
        worker=worker_profile,
        status='PENDING'
    ).select_related('customer', 'service').order_by('-created_at')[:5]

    active_bookings = Booking.objects.filter(
        worker=worker_profile,
        status__in=['ACCEPTED', 'IN_PROGRESS']
    ).select_related('customer', 'service').order_by('-created_at')[:5]

    completed_bookings = Booking.objects.filter(
        worker=worker_profile,
        status='COMPLETED'
    ).count()

    total_earnings = Booking.objects.filter(
        worker=worker_profile,
        status='COMPLETED',
        is_paid=True
    ).aggregate(total=Sum('estimated_cost'))['total'] or 0

    avg_rating = Review.objects.filter(
        worker=worker_profile
    ).aggregate(avg=Avg('rating'))['avg'] or 0

    notifications = Notification.objects.filter(
        recipient=request.user,
        is_read=False
    ).order_by('-created_at')[:5]

    context = {
        'worker_profile': worker_profile,
        'pending_bookings': pending_bookings,
        'active_bookings': active_bookings,
        'completed_bookings': completed_bookings,
        'total_earnings': total_earnings,
        'avg_rating': round(avg_rating, 1),
        'notifications': notifications,
    }

    return render(
        request,
        'dashboards/worker_dashboard.html',
        context
    )


@never_cache
def user_dashboard(request):

    if not request.user.is_authenticated:
        return redirect('login')

    if request.user.role != 'USER':
        return redirect('dashboard')

    try:
        profile = request.user.user_profile
    except UserProfile.DoesNotExist:
        profile = UserProfile.objects.create(user=request.user)

    bookings = Booking.objects.filter(
        customer=request.user
    ).select_related('worker', 'worker__user', 'service').order_by('-created_at')[:5]

    total_bookings = Booking.objects.filter(customer=request.user).count()
    completed_bookings = Booking.objects.filter(customer=request.user, status='COMPLETED').count()
    pending_bookings = Booking.objects.filter(
        customer=request.user,
        status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS']
    ).count()

    notifications = Notification.objects.filter(
        recipient=request.user,
        is_read=False
    ).order_by('-created_at')[:5]

    context = {
        'profile': profile,
        'bookings': bookings,
        'total_bookings': total_bookings,
        'completed_bookings': completed_bookings,
        'pending_bookings': pending_bookings,
        'notifications': notifications,
    }

    return render(
        request,
        'dashboards/user_dashboard.html',
        context
    )


@login_required
def edit_profile_view(request):
    if request.user.role == 'WORKER':
        return redirect('edit_worker_profile')

    try:
        profile = request.user.user_profile
    except UserProfile.DoesNotExist:
        profile = UserProfile.objects.create(user=request.user)

    if request.method == 'POST':
        form = UserProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            form.save()
            messages.success(request, 'Profile updated successfully.')
            return redirect('user_dashboard')
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = UserProfileForm(instance=profile)

    return render(request, 'user/edit_profile.html', {'form': form, 'profile': profile})


@login_required
def edit_worker_profile_view(request):
    if request.user.role != 'WORKER':
        return redirect('dashboard')

    try:
        profile = request.user.worker_profile
    except WorkerProfile.DoesNotExist:
        messages.error(request, 'Worker profile not found.')
        return redirect('dashboard')

    if request.method == 'POST':
        form = WorkerProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            form.save()
            messages.success(request, 'Profile updated successfully.')
            return redirect('worker_profile')
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = WorkerProfileForm(instance=profile)

    return render(request, 'workers/edit_worker_profile.html', {'form': form, 'profile': profile})


@login_required
def my_complaints_view(request):
    complaints = Complaint.objects.filter(
        customer=request.user
    ).select_related('booking', 'booking__service').order_by('-created_at')
    return render(request, 'user/my_complaints.html', {'complaints': complaints})


@login_required
def delete_account_view(request):
    if request.method != 'POST':
        return redirect('edit_profile')

    user = request.user
    logout(request)
    user.delete()
    messages.success(request, 'Your account has been permanently deleted.')
    return redirect('home')


@login_required
def service_update(request, pk):

    if not request.user.is_staff:
        return redirect('dashboard')

    service = get_object_or_404(
        Service,
        id=pk
    )

    if request.method == 'POST':

        form = ServiceForm(
            request.POST,
            request.FILES,
            instance=service
        )

        if form.is_valid():

            form.save()

            messages.success(
                request,
                'Service updated successfully.'
            )

            return redirect('admin_dashboard')

    else:

        form = ServiceForm(
            instance=service
        )

    return render(
        request,
        'lamo_service/service_form.html',
        {
            'form': form,
            'title': 'Edit Service',
            'service': service
        }
    )

@login_required
def service_delete(request, pk):

    # Only POST is allowed
    if request.method != 'POST':
        messages.error(
            request,
            'Invalid request method.'
        )
        return redirect('service_list')

    # Only ADMIN can delete services
    if request.user.role != 'ADMIN':
        messages.error(
            request,
            'You are not authorized to delete services.'
        )
        return redirect('dashboard')

    service = get_object_or_404(
        Service,
        pk=pk
    )

    service_name = service.name

    # Delete the service
    service.delete()

    messages.success(
        request,
        f'"{service_name}" deleted successfully.'
    )

    return redirect('service_list')
@login_required
def service_list(request):

    if request.user.role != 'ADMIN':
        return redirect('dashboard')

    services = Service.objects.all()

    return render(
        request,
        'lamo_service/service_list.html',
        {
            'services': services
        }
    )

def service_detail(request, pk):

    service = get_object_or_404(
        Service,
        pk=pk,
        is_active=True
    )

    return render(
        request,
        'lamo_service/service_detail.html',
        {
            'service': service
        }
    )

@login_required
def service_workers(request, pk):

    service = get_object_or_404(
        Service,
        pk=pk,
        is_active=True
    )

    district = request.GET.get('district', '').strip()

    workers = WorkerProfile.objects.select_related(
        'user',
        'service'
    ).filter(
        service=service,
        is_verified=True
    )

    if district:
        workers = workers.filter(
            district__iexact=district
        )

    return render(
        request,
        'lamo_service/service_workers.html',
        {
            'service': service,
            'district': district,
            'workers': workers,
        }
    )
def contact(request):
    return render(request,'contact.html')

def service_page(request):
    services = Service.objects.filter(
        is_active=True
    ).order_by('name')

    return render(
        request,
        'lamo_service/service_page.html',
        {
            'services': services
        }
    )
@login_required
def users_list(request):

    if request.user.role != 'ADMIN':
        return redirect('dashboard')

    users = CustomUser.objects.filter(
        role='USER'
    ).order_by('-date_joined')

    paginator = Paginator(users, 20)
    page = request.GET.get('page', 1)
    users_page = paginator.get_page(page)

    return render(
        request,
        'dashboards/users_list.html',
        {
            'users': users_page,
            'total_count': paginator.count,
        }
    )

@login_required
def workers_list(request):

    if request.user.role != 'ADMIN':
        return redirect('dashboard')

    workers = WorkerProfile.objects.select_related(
        'user',
        'service'
    ).all().order_by('-id')

    paginator = Paginator(workers, 20)
    page = request.GET.get('page', 1)
    workers_page = paginator.get_page(page)

    return render(
        request,
        'dashboards/workers_list.html',
        {
            'workers': workers_page,
            'total_count': paginator.count,
        }
    )
@login_required
def worker_verify(request, pk):

    # ADMIN ONLY
    if request.user.role != 'ADMIN':
        messages.error(
            request,
            'You are not authorized to verify workers.'
        )

        return redirect('dashboard')


    # ONLY POST
    if request.method != 'POST':

        messages.error(
            request,
            'Invalid request method.'
        )

        return redirect('workers_list')


    worker = get_object_or_404(
        WorkerProfile,
        pk=pk
    )


    # ALREADY VERIFIED

    if worker.is_verified:

        messages.info(
            request,
            f'{worker.user.username} is already verified.'
        )

        return redirect('workers_list')


    # VERIFY WORKER

    worker.is_verified = True

    worker.save(
        update_fields=[
            'is_verified',
            'updated_at'
        ]
    )


    messages.success(
        request,
        f'{worker.user.username} has been verified successfully.'
    )


    return redirect('workers_list')


@login_required
def worker_manage_dashboard(request):

    # ==========================================
    # WORKER ONLY
    # ==========================================

    if request.user.role != 'WORKER':

        messages.error(
            request,
            'You are not authorized to access the worker dashboard.'
        )

        return redirect('dashboard')


    # ==========================================
    # GET WORKER PROFILE
    # ==========================================

    try:

        worker_profile = request.user.worker_profile

    except WorkerProfile.DoesNotExist:

        messages.error(
            request,
            'Worker profile not found.'
        )

        return redirect('dashboard')


    # ==========================================
    # HANDLE POST
    # ==========================================

    if request.method == 'POST':

        action = request.POST.get('action')


        # ======================================
        # WORK STATUS
        # Available / On Duty / On Leave
        # ======================================

        if action == 'work_status':

            work_status = request.POST.get(
                'work_status',
                ''
            ).strip()


            # Check valid choice
            valid_values = [
                value
                for value, label
                in WorkerProfile.WORK_STATUS_CHOICES
            ]


            if work_status not in valid_values:

                messages.error(
                    request,
                    'Please select a valid work status.'
                )

                return redirect(
                    'worker_manage_dashboard'
                )


            # Update work status
            worker_profile.work_status = work_status

            worker_profile.save(
                update_fields=[
                    'work_status',
                    'updated_at'
                ]
            )


            # Success message
            if work_status == 'AVAILABLE':

                messages.success(
                    request,
                    'You are now AVAILABLE for bookings.'
                )

            elif work_status == 'ON_DUTY':

                messages.success(
                    request,
                    'Your status is now ON DUTY.'
                )

            elif work_status == 'LEAVE':

                messages.success(
                    request,
                    'Your status is now ON LEAVE.'
                )


            return redirect(
                'worker_manage_dashboard'
            )


        # ======================================
        # AVAILABILITY
        # Full Time / Part Time / Weekends / Flexible
        # ======================================

        elif action == 'availability':

            availability = request.POST.get(
                'availability',
                ''
            ).strip()


            # Check valid choice
            valid_values = [
                value
                for value, label
                in WorkerProfile.AVAILABILITY_CHOICES
            ]


            if availability not in valid_values:

                messages.error(
                    request,
                    'Please select a valid availability.'
                )

                return redirect(
                    'worker_manage_dashboard'
                )


            # Update availability
            worker_profile.availability = availability

            worker_profile.save(
                update_fields=[
                    'availability',
                    'updated_at'
                ]
            )


            messages.success(
                request,
                f'Availability updated to {availability}.'
            )


            return redirect(
                'worker_manage_dashboard'
            )


    # ==========================================
    # DASHBOARD DATA
    # ==========================================

    context = {

        # Worker profile
        'worker_profile': worker_profile,


        # Normal working availability choices
        'availability_choices':
            WorkerProfile.AVAILABILITY_CHOICES,


        # Current work status choices
        'work_status_choices':
            WorkerProfile.WORK_STATUS_CHOICES,


        # ======================================
        # JOB STATISTICS
        # ======================================

        # These can be connected to Booking later
        'total_jobs': 0,

        'pending_jobs': 0,

        'completed_jobs': 0,

        'total_earnings': 0,


        # Jobs
        'jobs': [],
    }


    # ==========================================
    # RENDER
    # ==========================================

    return render(
        request,
        'workers/worker_manage_dashboard.html',
        context
    )
@never_cache
@login_required
def notifications_view(request):

    notifications = Notification.objects.filter(
        recipient=request.user
    )

    return render(
        request,
        'notifications.html',
        {
            'notifications': notifications
        }
    )

@never_cache
@login_required
def worker_jobs(request):

    if request.user.role != 'WORKER':
        return redirect('dashboard')

    worker = get_object_or_404(
        WorkerProfile.objects.select_related(
            'user',
            'service'
        ),
        user=request.user
    )

    bookings = Booking.objects.filter(
        worker=worker
    ).select_related(
        'customer',
        'service'
    ).order_by(
        '-date',
        '-time'
    )

    return render(
        request,
        'workers/worker_jobs.html',
        {
            'worker_profile': worker,
            'bookings': bookings,
        }
    )


@never_cache
@login_required
def worker_profile(request):

    if request.user.role != 'WORKER':
        return redirect('dashboard')

    worker = get_object_or_404(
        WorkerProfile.objects.select_related(
            'user',
            'service'
        ),
        user=request.user
    )

    return render(
        request,
        'workers/worker_profile.html',
        {
            'worker_profile': worker,
        }
    )



@never_cache
@login_required
def worker_notifications(request):

    # ==========================================
    # WORKER ONLY
    # ==========================================

    if request.user.role != 'WORKER':
        messages.error(
            request,
            'You are not authorized to access notifications.'
        )
        return redirect('dashboard')


    # ==========================================
    # WORKER PROFILE
    # ==========================================

    worker = get_object_or_404(
        WorkerProfile.objects.select_related(
            'user',
            'service'
        ),
        user=request.user
    )


    # ==========================================
    # GET WORKER NOTIFICATIONS
    # ==========================================

    notifications = (
        Notification.objects
        .filter(
            recipient=request.user
        )
        .select_related(
            'booking',
            'booking__customer',
            'booking__service'
        )
        .order_by('-created_at')
    )


    # ==========================================
    # UNREAD COUNT
    # ==========================================

    unread_count = notifications.filter(
        is_read=False
    ).count()


    # ==========================================
    # RENDER
    # ==========================================

    return render(
        request,
        'workers/worker_notifications.html',
        {
            'worker_profile': worker,
            'notifications': notifications,
            'unread_count': unread_count,
        }
    )
# @login_required
    return render(
        request,
        'worker/worker_profile.html',
        {
            'worker_profile': worker_profile,
        }
    )
@login_required
def worker_update_status(request):

    if request.user.role != 'WORKER':
        return redirect('dashboard')

    worker = get_object_or_404(
        WorkerProfile,
        user=request.user
    )

    if request.method == 'POST':

        work_status = request.POST.get('work_status')

        valid_statuses = [
            value
            for value, label
            in WorkerProfile.WORK_STATUS_CHOICES
        ]

        if work_status not in valid_statuses:

            messages.error(
                request,
                'Invalid work status.'
            )

            return redirect('worker_dashboard')

        worker.work_status = work_status

        worker.save(
            update_fields=[
                'work_status',
                'updated_at'
            ]
        )

        messages.success(
            request,
            f'Work status updated to '
            f'{worker.get_work_status_display()}.'
        )

    return redirect('worker_dashboard')

@never_cache
@login_required
def my_bookings(request):

    # Workers should not access the customer booking page
    if request.user.role == 'WORKER':
        return redirect('worker_jobs')

    bookings = (
        Booking.objects
        .filter(customer=request.user)
        .select_related(
            'worker__user',
            'worker__service',
            'service'
        )
        .order_by('-created_at')
    )

    return render(
        request,
        'user/my_bookings.html',
        {
            'bookings': bookings,
        }
    )
@never_cache
@login_required
def admin_notifications(request):

    # ==========================================
    # ADMIN ONLY
    # ==========================================

    if request.user.role != 'ADMIN':

        messages.error(
            request,
            'You are not authorized to access admin notifications.'
        )

        return redirect('dashboard')


    # ==========================================
    # GET ADMIN NOTIFICATIONS
    # ==========================================

    notifications = Notification.objects.filter(
        recipient=request.user
    ).select_related(
        'booking',
        'booking__customer',
        'booking__worker__user',
        'booking__service'
    ).order_by(
        '-created_at'
    )


    # ==========================================
    # UNREAD COUNT
    # ==========================================

    unread_count = notifications.filter(
        is_read=False
    ).count()


    return render(
        request,
        'admin/notifications.html',
        {
            'notifications': notifications,
            'unread_count': unread_count,
        }
    )

@login_required
def confirm_completion(request, booking_id):

    # ==========================================
    # POST ONLY
    # ==========================================

    if request.method != 'POST':
        return redirect('my_bookings')


    # ==========================================
    # GET CUSTOMER'S BOOKING
    # ==========================================

    booking = get_object_or_404(
        Booking,
        id=booking_id,
        customer=request.user
    )


    # ==========================================
    # CHECK STATUS
    # ==========================================

    if booking.status != 'COMPLETION_REQUESTED':

        messages.error(
            request,
            'This booking is not waiting for completion confirmation.'
        )

        return redirect('my_bookings')


    # ==========================================
    # COMPLETION REQUESTED → COMPLETED
    # ==========================================

    booking.status = 'COMPLETED'

    booking.save(
        update_fields=[
            'status',
            'updated_at'
        ]
    )


    # ==========================================
    # NOTIFY WORKER + SEND EMAIL
    # ==========================================

    if booking.worker:

        Notification.objects.create(
            recipient=booking.worker.user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=(
                f'The customer has confirmed that the '
                f'{booking.service.name} work is complete.'
            )
        )

        try:
            send_mail(
                subject=f'LAMO - Job Completed! Booking #{booking.id}',
                message=(
                    f'Hi {booking.worker.user.username},\n\n'
                    f'Great news! The customer has confirmed that your {booking.service.name} '
                    f'work for Booking #{booking.id} is complete.\n\n'
                    f'Payment will be processed shortly.\n\n'
                    f'Thanks for your work!\nLAMO Team'
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[booking.worker.user.email],
                fail_silently=True,
            )
        except Exception:
            pass


    # ==========================================
    # SUCCESS MESSAGE
    # ==========================================

    messages.success(
        request,
        'Work confirmed successfully. You can now leave a review.'
    )


    # ==========================================
    # BACK TO MY BOOKINGS
    # ==========================================

    return redirect('my_bookings')
@login_required

@login_required
def service_update(request, pk):

    if not request.user.is_staff:
        return redirect('dashboard')

    service = get_object_or_404(
        Service,
        id=pk
    )

    if request.method == 'POST':

        form = ServiceForm(
            request.POST,
            request.FILES,
            instance=service
        )

        if form.is_valid():

            form.save()

            messages.success(
                request,
                'Service updated successfully.'
            )

            return redirect('admin_dashboard')

    else:

        form = ServiceForm(
            instance=service
        )

    return render(
        request,
        'lamo_service/service_form.html',
        {
            'form': form,
            'title': 'Edit Service',
            'service': service
        }
    )

@login_required
def service_delete(request, pk):

    # Only POST is allowed
    if request.method != 'POST':
        messages.error(
            request,
            'Invalid request method.'
        )
        return redirect('service_list')

    # Only ADMIN can delete services
    if request.user.role != 'ADMIN':
        messages.error(
            request,
            'You are not authorized to delete services.'
        )
        return redirect('dashboard')

    service = get_object_or_404(
        Service,
        pk=pk
    )

    service_name = service.name

    # Delete the service
    service.delete()

    messages.success(
        request,
        f'"{service_name}" deleted successfully.'
    )

    return redirect('service_list')
@login_required
def service_list(request):

    if request.user.role != 'ADMIN':
        return redirect('dashboard')

    services = Service.objects.all()

    return render(
        request,
        'lamo_service/service_list.html',
        {
            'services': services
        }
    )

def service_detail(request, pk):

    service = get_object_or_404(
        Service,
        pk=pk,
        is_active=True
    )

    return render(
        request,
        'lamo_service/service_detail.html',
        {
            'service': service
        }
    )

@login_required
def service_workers(request, pk):

    service = get_object_or_404(
        Service,
        pk=pk,
        is_active=True
    )

    district = request.GET.get('district', '').strip()

    workers = WorkerProfile.objects.select_related(
        'user',
        'service'
    ).filter(
        service=service,
        is_verified=True
    )

    if district:
        workers = workers.filter(
            district__iexact=district
        )

    return render(
        request,
        'lamo_service/service_workers.html',
        {
            'service': service,
            'district': district,
            'workers': workers,
        }
    )
def contact(request):
    return render(request,'contact.html')

def service_page(request):
    services = Service.objects.filter(
        is_active=True
    ).order_by('name')

    return render(
        request,
        'lamo_service/service_page.html',
        {
            'services': services
        }
    )
@login_required
def users_list(request):

    if request.user.role != 'ADMIN':
        return redirect('dashboard')

    users = CustomUser.objects.filter(
        role='USER'
    ).order_by('-date_joined')

    paginator = Paginator(users, 20)
    page = request.GET.get('page', 1)
    users_page = paginator.get_page(page)

    return render(
        request,
        'dashboards/users_list.html',
        {
            'users': users_page,
            'total_count': paginator.count,
        }
    )

@login_required
def workers_list(request):

    if request.user.role != 'ADMIN':
        return redirect('dashboard')

    workers = WorkerProfile.objects.select_related(
        'user',
        'service'
    ).all().order_by('-id')

    paginator = Paginator(workers, 20)
    page = request.GET.get('page', 1)
    workers_page = paginator.get_page(page)

    return render(
        request,
        'dashboards/workers_list.html',
        {
            'workers': workers_page,
            'total_count': paginator.count,
        }
    )
@login_required
def worker_verify(request, pk):

    # ADMIN ONLY
    if request.user.role != 'ADMIN':
        messages.error(
            request,
            'You are not authorized to verify workers.'
        )

        return redirect('dashboard')


    # ONLY POST
    if request.method != 'POST':

        messages.error(
            request,
            'Invalid request method.'
        )

        return redirect('workers_list')


    worker = get_object_or_404(
        WorkerProfile,
        pk=pk
    )


    # ALREADY VERIFIED

    if worker.is_verified:

        messages.info(
            request,
            f'{worker.user.username} is already verified.'
        )

        return redirect('workers_list')


    # VERIFY WORKER

    worker.is_verified = True

    worker.save(
        update_fields=[
            'is_verified',
            'updated_at'
        ]
    )


    messages.success(
        request,
        f'{worker.user.username} has been verified successfully.'
    )


    return redirect('workers_list')


@login_required
def worker_manage_dashboard(request):

    # ==========================================
    # WORKER ONLY
    # ==========================================

    if request.user.role != 'WORKER':

        messages.error(
            request,
            'You are not authorized to access the worker dashboard.'
        )

        return redirect('dashboard')


    # ==========================================
    # GET WORKER PROFILE
    # ==========================================

    try:

        worker_profile = request.user.worker_profile

    except WorkerProfile.DoesNotExist:

        messages.error(
            request,
            'Worker profile not found.'
        )

        return redirect('dashboard')


    # ==========================================
    # HANDLE POST
    # ==========================================

    if request.method == 'POST':

        action = request.POST.get('action')


        # ======================================
        # WORK STATUS
        # Available / On Duty / On Leave
        # ======================================

        if action == 'work_status':

            work_status = request.POST.get(
                'work_status',
                ''
            ).strip()


            # Check valid choice
            valid_values = [
                value
                for value, label
                in WorkerProfile.WORK_STATUS_CHOICES
            ]


            if work_status not in valid_values:

                messages.error(
                    request,
                    'Please select a valid work status.'
                )

                return redirect(
                    'worker_manage_dashboard'
                )


            # Update work status
            worker_profile.work_status = work_status

            worker_profile.save(
                update_fields=[
                    'work_status',
                    'updated_at'
                ]
            )


            # Success message
            if work_status == 'AVAILABLE':

                messages.success(
                    request,
                    'You are now AVAILABLE for bookings.'
                )

            elif work_status == 'ON_DUTY':

                messages.success(
                    request,
                    'Your status is now ON DUTY.'
                )

            elif work_status == 'LEAVE':

                messages.success(
                    request,
                    'Your status is now ON LEAVE.'
                )


            return redirect(
                'worker_manage_dashboard'
            )


        # ======================================
        # AVAILABILITY
        # Full Time / Part Time / Weekends / Flexible
        # ======================================

        elif action == 'availability':

            availability = request.POST.get(
                'availability',
                ''
            ).strip()


            # Check valid choice
            valid_values = [
                value
                for value, label
                in WorkerProfile.AVAILABILITY_CHOICES
            ]


            if availability not in valid_values:

                messages.error(
                    request,
                    'Please select a valid availability.'
                )

                return redirect(
                    'worker_manage_dashboard'
                )


            # Update availability
            worker_profile.availability = availability

            worker_profile.save(
                update_fields=[
                    'availability',
                    'updated_at'
                ]
            )


            messages.success(
                request,
                f'Availability updated to {availability}.'
            )


            return redirect(
                'worker_manage_dashboard'
            )


    # ==========================================
    # DASHBOARD DATA
    # ==========================================

    context = {

        # Worker profile
        'worker_profile': worker_profile,


        # Normal working availability choices
        'availability_choices':
            WorkerProfile.AVAILABILITY_CHOICES,


        # Current work status choices
        'work_status_choices':
            WorkerProfile.WORK_STATUS_CHOICES,


        # ======================================
        # JOB STATISTICS
        # ======================================

        # These can be connected to Booking later
        'total_jobs': 0,

        'pending_jobs': 0,

        'completed_jobs': 0,

        'total_earnings': 0,


        # Jobs
        'jobs': [],
    }


    # ==========================================
    # RENDER
    # ==========================================

    return render(
        request,
        'workers/worker_manage_dashboard.html',
        context
    )
@never_cache
@login_required
def notifications_view(request):

    notifications = Notification.objects.filter(
        recipient=request.user
    )

    return render(
        request,
        'notifications.html',
        {
            'notifications': notifications
        }
    )

@never_cache
@login_required
def worker_jobs(request):

    if request.user.role != 'WORKER':
        return redirect('dashboard')

    worker = get_object_or_404(
        WorkerProfile.objects.select_related(
            'user',
            'service'
        ),
        user=request.user
    )

    bookings = Booking.objects.filter(
        worker=worker
    ).select_related(
        'customer',
        'service'
    ).order_by(
        '-date',
        '-time'
    )

    return render(
        request,
        'workers/worker_jobs.html',
        {
            'worker_profile': worker,
            'bookings': bookings,
        }
    )


@never_cache
@login_required
def worker_profile(request):

    if request.user.role != 'WORKER':
        return redirect('dashboard')

    worker = get_object_or_404(
        WorkerProfile.objects.select_related(
            'user',
            'service'
        ),
        user=request.user
    )

    return render(
        request,
        'workers/worker_profile.html',
        {
            'worker_profile': worker,
        }
    )



@never_cache
@login_required
def worker_notifications(request):

    # ==========================================
    # WORKER ONLY
    # ==========================================

    if request.user.role != 'WORKER':
        messages.error(
            request,
            'You are not authorized to access notifications.'
        )
        return redirect('dashboard')


    # ==========================================
    # WORKER PROFILE
    # ==========================================

    worker = get_object_or_404(
        WorkerProfile.objects.select_related(
            'user',
            'service'
        ),
        user=request.user
    )


    # ==========================================
    # GET WORKER NOTIFICATIONS
    # ==========================================

    notifications = (
        Notification.objects
        .filter(
            recipient=request.user
        )
        .select_related(
            'booking',
            'booking__customer',
            'booking__service'
        )
        .order_by('-created_at')
    )


    # ==========================================
    # UNREAD COUNT
    # ==========================================

    unread_count = notifications.filter(
        is_read=False
    ).count()


    # ==========================================
    # RENDER
    # ==========================================

    return render(
        request,
        'workers/worker_notifications.html',
        {
            'worker_profile': worker,
            'notifications': notifications,
            'unread_count': unread_count,
        }
    )
# @login_required
# def worker_profile(request):

#     if request.user.role != 'WORKER':
#         return redirect('dashboard')

#     worker_profile = get_object_or_404(
#         WorkerProfile.objects.select_related(
#             'user',
#             'service'
#         ),
#         user=request.user
#     )

    return render(
        request,
        'worker/worker_profile.html',
        {
            'worker_profile': worker_profile,
        }
    )
@login_required
def worker_update_status(request):

    if request.user.role != 'WORKER':
        return redirect('dashboard')

    worker = get_object_or_404(
        WorkerProfile,
        user=request.user
    )

    if request.method == 'POST':

        work_status = request.POST.get('work_status')

        valid_statuses = [
            value
            for value, label
            in WorkerProfile.WORK_STATUS_CHOICES
        ]

        if work_status not in valid_statuses:

            messages.error(
                request,
                'Invalid work status.'
            )

            return redirect('worker_dashboard')

        worker.work_status = work_status

        worker.save(
            update_fields=[
                'work_status',
                'updated_at'
            ]
        )

        messages.success(
            request,
            f'Work status updated to '
            f'{worker.get_work_status_display()}.'
        )

    return redirect('worker_dashboard')

@never_cache
@login_required
def my_bookings(request):

    # Workers should not access the customer booking page
    if request.user.role == 'WORKER':
        return redirect('worker_jobs')

    bookings = (
        Booking.objects
        .filter(customer=request.user)
        .select_related(
            'worker__user',
            'worker__service',
            'service'
        )
        .order_by('-created_at')
    )

    return render(
        request,
        'user/my_bookings.html',
        {
            'bookings': bookings,
        }
    )
@never_cache
@login_required
def admin_notifications(request):

    # ==========================================
    # ADMIN ONLY
    # ==========================================

    if request.user.role != 'ADMIN':

        messages.error(
            request,
            'You are not authorized to access admin notifications.'
        )

        return redirect('dashboard')


    # ==========================================
    # GET ADMIN NOTIFICATIONS
    # ==========================================

    notifications = Notification.objects.filter(
        recipient=request.user
    ).select_related(
        'booking',
        'booking__customer',
        'booking__worker__user',
        'booking__service'
    ).order_by(
        '-created_at'
    )


    # ==========================================
    # UNREAD COUNT
    # ==========================================

    unread_count = notifications.filter(
        is_read=False
    ).count()


    return render(
        request,
        'admin/notifications.html',
        {
            'notifications': notifications,
            'unread_count': unread_count,
        }
    )

@login_required
def confirm_completion(request, booking_id):

    # ==========================================
    # POST ONLY
    # ==========================================

    if request.method != 'POST':
        return redirect('my_bookings')


    # ==========================================
    # GET CUSTOMER'S BOOKING
    # ==========================================

    booking = get_object_or_404(
        Booking,
        id=booking_id,
        customer=request.user
    )


    # ==========================================
    # CHECK STATUS
    # ==========================================

    if booking.status != 'COMPLETION_REQUESTED':

        messages.error(
            request,
            'This booking is not waiting for completion confirmation.'
        )

        return redirect('my_bookings')


    # ==========================================
    # COMPLETION REQUESTED → COMPLETED
    # ==========================================

    booking.status = 'COMPLETED'

    booking.save(
        update_fields=[
            'status',
            'updated_at'
        ]
    )


    # ==========================================
    # NOTIFY WORKER + SEND EMAIL
    # ==========================================

    if booking.worker:

        Notification.objects.create(
            recipient=booking.worker.user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=(
                f'The customer has confirmed that the '
                f'{booking.service.name} work is complete.'
            )
        )

        try:
            send_mail(
                subject=f'LAMO - Job Completed! Booking #{booking.id}',
                message=(
                    f'Hi {booking.worker.user.username},\n\n'
                    f'Great news! The customer has confirmed that your {booking.service.name} '
                    f'work for Booking #{booking.id} is complete.\n\n'
                    f'Payment will be processed shortly.\n\n'
                    f'Thanks for your work!\nLAMO Team'
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[booking.worker.user.email],
                fail_silently=True,
            )
        except Exception:
            pass


    # ==========================================
    # SUCCESS MESSAGE
    # ==========================================

    messages.success(
        request,
        'Work confirmed successfully. You can now leave a review.'
    )


    # ==========================================
    # BACK TO MY BOOKINGS
    # ==========================================

    return redirect('my_bookings')
@login_required
# @login_required
# def raise_complaint(request, booking_id):

#     if request.user.role == 'WORKER':
#         messages.error(
#             request,
#             'Workers cannot raise customer complaints.'
#         )
#         return redirect('worker_jobs')

#     booking = get_object_or_404(
#         Booking,
#         id=booking_id,
#         customer=request.user
#     )

#     if booking.status not in ['COMPLETION_REQUESTED', 'COMPLETED']:
#         messages.error(
#             request,
#             'You can raise a complaint only for completion requested or completed bookings.'
#         )
#         return redirect('my_bookings')

#     if request.method == 'POST':

#         subject = request.POST.get(
#             'subject',
#             ''
#         ).strip()

#         description = request.POST.get(
#             'description',
#             ''
#         ).strip()

#         if not subject or not description:

#             messages.error(
#                 request,
#                 'Please provide both subject and description.'
#             )

#             return render(
#                 request,
#                 'users/raise_complaint.html',
#                 {
#                     'booking': booking
#                 }
#             )

#         complaint = Complaint.objects.create(
#             booking=booking,
#             customer=request.user,
#             subject=subject,
#             description=description
#         )

#         try:
#             from .ai_utils import classify_complaint_severity
#             complaint.severity = classify_complaint_severity(subject, description)
#             complaint.save(update_fields=['severity'])
#         except Exception:
#             pass

#         admin_users = CustomUser.objects.filter(
#             role='ADMIN'
#         )

#         for admin in admin_users:

#             Notification.objects.create(
#                 recipient=admin,
#                 booking=booking,
#                 notification_type='COMPLAINT',
#                 message=(
#                     f'New complaint raised for '
#                     f'Booking #{booking.id}.'
#                 )
#             )

#         messages.success(
#             request,
#             'Your complaint has been submitted successfully.'
#         )

#         return redirect('my_bookings')

#     return render(
#         request,
#         'users/raise_complaint.html',
#         {
#             'booking': booking
#         }
#     )

@login_required
def worker_start_job(request, booking_id):

    if request.user.role != 'WORKER':
        return redirect('dashboard')

    if request.method != 'POST':
        return redirect('worker_dashboard')

    booking = get_object_or_404(
        Booking,
        id=booking_id,
        worker__user=request.user
    )

    if booking.status != 'ACCEPTED':
        messages.error(
            request,
            'This job cannot be started at this stage.'
        )
        return redirect('worker_dashboard')

    booking.status = 'IN_PROGRESS'
    booking.save(update_fields=['status'])

    messages.success(
        request,
        'Job started successfully.'
    )

    return redirect('worker_dashboard')

@login_required
def worker_request_completion(request, booking_id):

    # ==========================================
    # WORKER ONLY
    # ==========================================

    if request.user.role != 'WORKER':
        messages.error(
            request,
            'You are not authorized.'
        )
        return redirect('dashboard')


    # ==========================================
    # POST ONLY
    # ==========================================

    if request.method != 'POST':
        return redirect('worker_jobs')


    # ==========================================
    # GET WORKER PROFILE
    # ==========================================

    worker = get_object_or_404(
        WorkerProfile,
        user=request.user
    )


    # ==========================================
    # GET THIS WORKER'S BOOKING
    # ==========================================

    booking = get_object_or_404(
        Booking,
        id=booking_id,
        worker=worker
    )


    # ==========================================
    # ONLY IN-PROGRESS JOB CAN REQUEST COMPLETION
    # ==========================================

    if booking.status != 'IN_PROGRESS':

        messages.warning(
            request,
            'Only an in-progress job can request completion.'
        )

        return redirect('worker_jobs')


    # ==========================================
    # CHANGE STATUS
    # IN_PROGRESS → COMPLETION_REQUESTED
    # ==========================================

    booking.status = 'COMPLETION_REQUESTED'

    booking.save(
        update_fields=[
            'status',
            'updated_at'
        ]
    )


    # ==========================================
    # NOTIFY CUSTOMER + SEND EMAIL
    # ==========================================

    Notification.objects.create(
        recipient=booking.customer,
        booking=booking,
        notification_type='BOOKING_UPDATE',
        message=(
            f'Your {booking.service.name} booking has been '
            f'marked as completed by the worker. '
            f'Please verify the work.'
        )
    )

    try:
        send_mail(
            subject=f'LAMO - Worker Completed Job (#{booking.id})',
            message=(
                f'Hi {booking.customer.username},\n\n'
                f'The worker has marked your {booking.service.name} job as complete.\n\n'
                f'Please login and verify the work to finalize the booking.\n\n'
                f'Thanks,\nLAMO Team'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[booking.customer.email],
            fail_silently=True,
        )
    except Exception:
        pass


    # ==========================================
    # SUCCESS MESSAGE
    # ==========================================

    messages.success(
        request,
        'Completion request sent to the customer.'
    )


    return redirect('worker_jobs')

@login_required
def submit_review(request, booking_id):

    # ==========================================
    # CUSTOMER ONLY
    # ==========================================

    if request.user.role == 'WORKER':

        messages.error(
            request,
            'Workers cannot submit reviews.'
        )

        return redirect('dashboard')


    # ==========================================
    # GET CUSTOMER BOOKING
    # ==========================================

    booking = get_object_or_404(
        Booking,
        id=booking_id,
        customer=request.user
    )


    # ==========================================
    # ONLY COMPLETED BOOKINGS
    # ==========================================

    if booking.status != 'COMPLETED':

        messages.error(
            request,
            'You can review only completed bookings.'
        )

        return redirect('my_bookings')


    # ==========================================
    # CHECK EXISTING REVIEW
    # ==========================================

    existing_review = Review.objects.filter(
        booking=booking
    ).first()


    if existing_review:

        messages.info(
            request,
            'You have already reviewed this booking.'
        )

        return redirect('my_bookings')


    # ==========================================
    # SUBMIT REVIEW
    # ==========================================

    if request.method == 'POST':

        rating = request.POST.get('rating')
        comment = request.POST.get(
            'comment',
            ''
        ).strip()


        # --------------------------------------
        # VALIDATE RATING
        # --------------------------------------

        try:

            rating = int(rating)

        except (TypeError, ValueError):

            messages.error(
                request,
                'Please select a valid rating.'
            )

            return render(
                request,
                'user/submit_review.html',
                {
                    'booking': booking
                }
            )


        if rating < 1 or rating > 5:

            messages.error(
                request,
                'Rating must be between 1 and 5 stars.'
            )

            return render(
                request,
                'user/submit_review.html',
                {
                    'booking': booking
                }
            )


        # --------------------------------------
        # CREATE REVIEW
        # --------------------------------------

        Review.objects.create(
            booking=booking,
            customer=request.user,
            worker=booking.worker,
            rating=rating,
            comment=comment
        )


        # --------------------------------------
        # NOTIFY WORKER
        # --------------------------------------

        if booking.worker:

            Notification.objects.create(
                recipient=booking.worker.user,
                booking=booking,
                notification_type='BOOKING_UPDATE',
                message=(
                    f'{request.user.get_full_name() or request.user.username} '
                    f'left a {rating}-star review for your '
                    f'{booking.service.name} service.'
                )
            )


        messages.success(
            request,
            'Thank you! Your review has been submitted successfully.'
        )


        return redirect('my_bookings')


    # ==========================================
    # SHOW REVIEW PAGE
    # ==========================================

    return render(
        request,
        'user/submit_review.html',
        {
            'booking': booking
        }
    )


@login_required
def submit_feedback(request, booking_id):
    if request.user.role == 'WORKER':
        messages.error(request, 'Workers cannot submit feedback.')
        return redirect('dashboard')

    booking = get_object_or_404(
        Booking,
        id=booking_id,
        customer=request.user
    )

    if booking.status != 'COMPLETED':
        messages.error(request, 'You can submit feedback only for completed bookings.')
        return redirect('my_bookings')

    existing_review = Review.objects.filter(booking=booking).first()
    existing_complaint = Complaint.objects.filter(booking=booking, customer=request.user).first()

    if request.method == 'POST':
        rating = request.POST.get('rating')
        comment = request.POST.get('comment', '').strip()
        complaint_category = request.POST.get('complaint_category', '').strip()
        complaint_description = request.POST.get('complaint_description', '').strip()

        has_review = bool(rating)
        has_complaint = bool(complaint_category or complaint_description)

        if not has_review and not has_complaint:
            messages.error(request, 'Please provide a rating/complaint or both.')
            return render(request, 'user/submit_feedback.html', {
                'booking': booking,
                'existing_review': existing_review,
                'existing_complaint': existing_complaint,
            })

        if has_review:
            if existing_review:
                messages.info(request, 'You have already reviewed this booking.')
            else:
                try:
                    rating = int(rating)
                except (TypeError, ValueError):
                    messages.error(request, 'Please select a valid rating.')
                    return render(request, 'user/submit_feedback.html', {
                        'booking': booking,
                        'existing_review': existing_review,
                        'existing_complaint': existing_complaint,
                    })

                if rating < 1 or rating > 5:
                    messages.error(request, 'Rating must be between 1 and 5 stars.')
                    return render(request, 'user/submit_feedback.html', {
                        'booking': booking,
                        'existing_review': existing_review,
                        'existing_complaint': existing_complaint,
                    })

                Review.objects.create(
                    booking=booking,
                    customer=request.user,
                    worker=booking.worker,
                    rating=rating,
                    comment=comment
                )

                if booking.worker:
                    Notification.objects.create(
                        recipient=booking.worker.user,
                        booking=booking,
                        notification_type='BOOKING_UPDATE',
                        message=(
                            f'{request.user.get_full_name() or request.user.username} '
                            f'left a {rating}-star review for your '
                            f'{booking.service.name} service.'
                        )
                    )

        if has_complaint:
            if existing_complaint:
                messages.info(request, 'You have already raised a complaint for this booking.')
            else:
                if not complaint_category:
                    messages.error(request, 'Please select a complaint category.')
                    return render(request, 'user/submit_feedback.html', {
                        'booking': booking,
                        'existing_review': existing_review,
                        'existing_complaint': existing_complaint,
                    })

                if not complaint_description:
                    messages.error(request, 'Please describe your complaint.')
                    return render(request, 'user/submit_feedback.html', {
                        'booking': booking,
                        'existing_review': existing_review,
                        'existing_complaint': existing_complaint,
                    })

                complaint = Complaint.objects.create(
                    booking=booking,
                    customer=request.user,
                    subject=complaint_category,
                    description=complaint_description,
                    status='OPEN'
                )

                try:
                    from .ai_utils import classify_complaint_severity
                    severity = classify_complaint_severity(complaint_category, complaint_description)
                    if severity:
                        complaint.severity = severity
                        complaint.save(update_fields=['severity'])
                except Exception:
                    pass

                Notification.objects.create(
                    recipient=request.user,
                    booking=booking,
                    notification_type='SYSTEM',
                    message=f'Your complaint for Booking #{booking.id} has been submitted successfully.'
                )

        messages.success(request, 'Thank you! Your feedback has been submitted successfully.')
        return redirect('my_bookings')

    return render(request, 'user/submit_feedback.html', {
        'booking': booking,
        'existing_review': existing_review,
        'existing_complaint': existing_complaint,
    })


@login_required
def worker_leave_management(request):
    if request.user.role != 'WORKER':
        messages.error(request, 'Only workers can manage leave.')
        return redirect('dashboard')

    worker_profile = get_object_or_404(WorkerProfile, user=request.user)
    leaves = WorkerLeave.objects.filter(worker=worker_profile).order_by('-leave_date')

    if request.method == 'POST':
        leave_date_str = request.POST.get('leave_date', '').strip()
        reason = request.POST.get('reason', '').strip()

        if not leave_date_str:
            messages.error(request, 'Please select a leave date.')
            return render(request, 'workers/worker_leave.html', {
                'leaves': leaves,
            })

        from datetime import datetime, date
        try:
            leave_date = datetime.strptime(leave_date_str, '%Y-%m-%d').date()
        except ValueError:
            messages.error(request, 'Invalid date format.')
            return render(request, 'workers/worker_leave.html', {
                'leaves': leaves,
            })

        today = date.today()
        if leave_date < today:
            messages.error(request, 'Cannot create leave for past dates.')
            return render(request, 'workers/worker_leave.html', {
                'leaves': leaves,
            })

        if WorkerLeave.objects.filter(worker=worker_profile, leave_date=leave_date).exists():
            messages.warning(request, 'You already have leave on this date.')
            return render(request, 'workers/worker_leave.html', {
                'leaves': leaves,
            })

        WorkerLeave.objects.create(
            worker=worker_profile,
            leave_date=leave_date,
            reason=reason
        )

        messages.success(request, 'Leave added successfully.')
        return redirect('worker_leave')

    return render(request, 'workers/worker_leave.html', {
        'leaves': leaves,
    })


@login_required
def worker_leave_delete(request, leave_id):
    if request.user.role != 'WORKER':
        messages.error(request, 'Only workers can manage leave.')
        return redirect('dashboard')

    worker_profile = get_object_or_404(WorkerProfile, user=request.user)
    leave = get_object_or_404(WorkerLeave, id=leave_id, worker=worker_profile)

    from datetime import date
    if leave.leave_date < date.today():
        messages.error(request, 'Cannot delete past leave.')
        return redirect('worker_leave')

    if request.method == 'POST':
        leave.delete()
        messages.success(request, 'Leave deleted successfully.')
        return redirect('worker_leave')

    return redirect('worker_leave')


@login_required
def admin_booking_details(request):

    # =====================================================
    # ADMIN ONLY
    # =====================================================

    if request.user.role != 'ADMIN':

        messages.error(
            request,
            'You are not authorized to view bookings.'
        )

        return redirect('dashboard')


    # =====================================================
    # GET BOOKINGS
    # =====================================================

    bookings = (
        Booking.objects
        .select_related(
            'customer',
            'worker__user',
            'worker__service',
            'service'
        )
        .order_by(
            '-created_at'
        )
    )


    # =====================================================
    # STATUS FILTER
    # =====================================================

    status = request.GET.get(
        'status',
        ''
    ).strip()


    if status:

        bookings = bookings.filter(
            status=status
        )


    # =====================================================
    # SEARCH
    # =====================================================

    search = request.GET.get(
        'search',
        ''
    ).strip()


    if search:

        bookings = bookings.filter(
            Q(customer__username__icontains=search)
            |
            Q(customer__first_name__icontains=search)
            |
            Q(customer__last_name__icontains=search)
            |
            Q(worker__user__username__icontains=search)
            |
            Q(worker__user__first_name__icontains=search)
            |
            Q(worker__user__last_name__icontains=search)
            |
            Q(service__name__icontains=search)
        )


    # =====================================================
    # RENDER
    # =====================================================

    return render(
        request,
        'admin/booking_details.html',
        {
            'bookings': bookings,
            'selected_status': status,
            'search': search,
        }
    )

@login_required
def admin_booking_view(request, booking_id):

    # ==========================================
    # ADMIN ONLY
    # ==========================================

    if request.user.role != 'ADMIN':

        messages.error(
            request,
            'You are not authorized to view booking details.'
        )

        return redirect('dashboard')


    # ==========================================
    # GET BOOKING
    # ==========================================

    booking = get_object_or_404(
        Booking.objects.select_related(
            'customer',
            'worker__user',
            'worker__service',
            'service'
        ),
        id=booking_id
    )


    # ==========================================
    # RENDER
    # ==========================================

    return render(
        request,
        'admin/admin_booking_view.html',
        {
            'booking': booking,
        }
    )

@login_required
def admin_reviews_complaints(request):

    if request.user.role != 'ADMIN':
        return redirect('dashboard')

    # ---------------------------------------------------------
    # FILTER
    # ---------------------------------------------------------

    selected_type = request.GET.get('type', 'all')
    search = request.GET.get('search', '').strip()

    # ---------------------------------------------------------
    # REVIEWS
    # ---------------------------------------------------------

    reviews = (
        Review.objects
        .select_related(
            'booking',
            'booking__service',
            'customer',
            'worker',
            'worker__user',
        )
        .order_by('-created_at')
    )

    # ---------------------------------------------------------
    # COMPLAINTS
    # ---------------------------------------------------------

    complaints = (
        Complaint.objects
        .select_related(
            'booking',
            'booking__service',
            'booking__worker',
            'booking__worker__user',
            'customer',
        )
        .annotate(
            severity_order=Case(
                When(severity='HIGH', then=Value(1)),
                When(severity='MEDIUM', then=Value(2)),
                When(severity='LOW', then=Value(3)),
                default=Value(2),
                output_field=IntegerField()
            )
        )
        .order_by('severity_order', '-created_at')
    )

    # ---------------------------------------------------------
    # SEARCH
    # ---------------------------------------------------------

    if search:

        reviews = reviews.filter(
            Q(customer__username__icontains=search) |
            Q(customer__first_name__icontains=search) |
            Q(customer__last_name__icontains=search) |
            Q(worker__user__username__icontains=search) |
            Q(worker__user__first_name__icontains=search) |
            Q(worker__user__last_name__icontains=search)
        )

        complaints = complaints.filter(
            Q(customer__username__icontains=search) |
            Q(customer__first_name__icontains=search) |
            Q(customer__last_name__icontains=search) |
            Q(booking__worker__user__username__icontains=search) |
            Q(booking__worker__user__first_name__icontains=search) |
            Q(booking__worker__user__last_name__icontains=search)
        )

    # ---------------------------------------------------------
    # COUNTS
    # ---------------------------------------------------------

    review_count = Review.objects.count()

    complaint_count = Complaint.objects.count()

    open_complaint_count = Complaint.objects.filter(
        status='OPEN'
    ).count()

    # ---------------------------------------------------------
    # CONTEXT
    # ---------------------------------------------------------

    context = {
        'reviews': reviews,
        'complaints': complaints,

        'review_count': review_count,
        'complaint_count': complaint_count,
        'open_complaint_count': open_complaint_count,

        'selected_type': selected_type,
        'search': search,
    }

    return render(
        request,
        'admin/reviews_complaints.html',
        context
    )


@login_required
def cancel_booking(request, booking_id):
    if request.method != 'POST':
        return redirect('my_bookings')

    booking = get_object_or_404(Booking, id=booking_id)

    if booking.customer != request.user:
        messages.error(request, "You are not authorized to cancel this booking.")
        return redirect('my_bookings')

    if booking.status not in ['PENDING', 'ACCEPTED']:
        messages.error(request, "This booking cannot be cancelled at its current status.")
        return redirect('my_bookings')

    booking.status = 'CANCELLED'
    booking.save(update_fields=['status', 'updated_at'])

    # Notify worker
    if booking.worker and booking.worker.user:
        Notification.objects.create(
            recipient=booking.worker.user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=f"Booking #{booking.id} for {booking.service.name} was cancelled by the customer."
        )

    # Notify admins
    admins = CustomUser.objects.filter(role='ADMIN', is_active=True)
    for admin_user in admins:
        Notification.objects.create(
            recipient=admin_user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=f"Booking #{booking.id} ({booking.service.name}) was cancelled by customer {request.user.username}."
        )

    # Send email to worker
    try:
        send_mail(
            subject=f'LAMO - Booking #{booking.id} Cancelled',
            message=(
                f'Hi {booking.worker.user.username},\n\n'
                f'Booking #{booking.id} for {booking.service.name} on {booking.date} '
                f'has been cancelled by the customer.\n\n'
                f'Thanks,\nLAMO Team'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[booking.worker.user.email],
            fail_silently=True,
        )
    except Exception:
        pass

    messages.success(request, f"Booking #{booking.id} has been cancelled successfully.")
    return redirect('my_bookings')


@login_required
def update_complaint_status(request, complaint_id):
    if request.user.role != 'ADMIN':
        messages.error(request, 'You are not authorized.')
        return redirect('dashboard')

    if request.method == 'POST':
        complaint = get_object_or_404(Complaint, id=complaint_id)
        status = request.POST.get('status', '').strip()
        admin_remark = request.POST.get('admin_remark', '').strip()

        valid_statuses = ['OPEN', 'UNDER_REVIEW', 'RESOLVED', 'REJECTED']
        if status in valid_statuses:
            complaint.status = status
            complaint.admin_remark = admin_remark
            complaint.save(update_fields=['status', 'admin_remark'])
            messages.success(request, f"Complaint #{complaint.id} status updated to {status}.")

            Notification.objects.create(
                recipient=complaint.customer,
                booking=complaint.booking,
                notification_type='SYSTEM',
                message=f"Your complaint regarding Booking #{complaint.booking.id} status was updated to '{status}'."
            )

            try:
                send_mail(
                    subject=f'LAMO - Complaint #{complaint.id} Update',
                    message=(
                        f'Hi {complaint.customer.username},\n\n'
                        f'Your complaint regarding Booking #{complaint.booking.id} '
                        f'has been updated to: {status}\n\n'
                        f'Admin Remark: {admin_remark or "No remarks"}\n\n'
                        f'Thanks,\nLAMO Team'
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[complaint.customer.email],
                    fail_silently=True,
                )
            except Exception:
                pass
        else:
            messages.error(request, 'Invalid complaint status.')

    return redirect('admin_reviews_complaints')


def ai_service_assistant(request):


    print("========== AI ASSISTANT CALLED ==========")
    print("USER:", request.user)
    print("AUTHENTICATED:", request.user.is_authenticated)
    print("METHOD:", request.method)
    # ==================================================
    # POST ONLY
    # ==================================================
    if request.method != 'POST':
        return JsonResponse({
            'success': False,
            'message': 'Invalid request method.'
        }, status=400)

    # ==================================================
    # GET MESSAGE
    # ==================================================
    try:
        import json

        if request.body:
            data = json.loads(
                request.body.decode('utf-8')
            )

            message = data.get(
                'message',
                ''
            ).strip()

            district = data.get(
                'district',
                ''
            ).strip()

        else:
            message = request.POST.get(
                'message',
                ''
            ).strip()

            district = request.POST.get(
                'district',
                ''
            ).strip()

    except Exception:
        message = request.POST.get(
            'message',
            ''
        ).strip()

        district = request.POST.get(
            'district',
            ''
        ).strip()

    # ==================================================
    # EMPTY MESSAGE
    # ==================================================
    if not message:
        return JsonResponse({
            'success': False,
            'message': 'Please enter a message.'
        })

    # ==================================================
    # GUEST USER
    # ==================================================
    # ==================================================
# GUEST USER
# ==================================================
    if not request.user.is_authenticated:

        lower_message = message.lower().strip()

        # --------------------------------------------------
        # GREETINGS
        # --------------------------------------------------
        greetings = [
            'hi',
            'hello',
            'hey',
            'helo',
            'hai',
            'hii',
            'good morning',
            'good afternoon',
            'good evening'
        ]

        if lower_message in greetings:

            return JsonResponse({
                'success': True,
                'type': 'chat',
                'message': (
                    "Hello! 👋 "
                    "How can I help you today?"
                )
            })

        # --------------------------------------------------
        # HOW ARE YOU
        # --------------------------------------------------
        elif 'how are you' in lower_message:

            return JsonResponse({
                'success': True,
                'type': 'chat',
                'message': (
                    "I'm doing great! 😊 "
                    "How can I help you with your "
                    "home service needs?"
                )
            })

        # --------------------------------------------------
        # SERVICES
        # --------------------------------------------------
        elif (
            'service' in lower_message
            or 'services' in lower_message
        ):

            services = Service.objects.filter(
                is_active=True
            ).values_list(
                'name',
                flat=True
            )

            service_list = ", ".join(services)

            return JsonResponse({
                'success': True,
                'type': 'chat',
                'message': (
                    f"We currently offer services such as "
                    f"{service_list}. "
                    "Tell me what work you need help with. 😊"
                )
            })

        # --------------------------------------------------
        # OTHER REQUESTS
        # --------------------------------------------------
        else:

            return JsonResponse({
                'success': True,
                'type': 'login_required',
                'message': (
                    "Sure! 😊 I can help you find the right "
                    "home service. Please login to view "
                    "available service providers and their details."
                )
            })
    # ==================================================
    # LOGGED-IN USER
    # ==================================================
    try:

        from .ai_utils import (
            chat_with_gemini,
            match_service_and_workers
        )

        # --------------------------------------------------
        # GENERAL CHAT
        # --------------------------------------------------
        lower_message = message.lower()

        greetings = [
            'hi',
            'hello',
            'hey',
            'helo',
            'hai',
            'hii'
        ]

        if lower_message in greetings:

            reply = chat_with_gemini(message)

            if not reply:
                reply = (
                    "Hello! 👋 "
                    "How can I help you today?"
                )

            return JsonResponse({
                'success': True,
                'type': 'chat',
                'message': reply
            })

        # --------------------------------------------------
        # SERVICE MATCHING
        # --------------------------------------------------
        res = match_service_and_workers(
            message,
            district=district
        )

        matched_service = res['service']
        workers = res['workers']

        if not matched_service:
            return JsonResponse({
                'success': False,
                'message': (
                    "Sorry, I couldn't find a matching "
                    "service for your request."
                )
            })

        # --------------------------------------------------
        # WORKER DATA
        # --------------------------------------------------
        workers_data = []

        for w in workers:

            workers_data.append({
                'id': w.id,

                'name': (
                    w.user.get_full_name()
                    or w.user.username
                ),

                'district': w.district,

                'experience': w.experience_years,

                'rating': (
                    round(w.avg_rating, 1)
                    if getattr(
                        w,
                        'avg_rating',
                        None
                    )
                    else 'New'
                ),

                'work_status': w.work_status,
            })

        # --------------------------------------------------
        # RESPONSE
        # --------------------------------------------------
        return JsonResponse({

            'success': True,

            'type': 'service_match',

            'service': {

                'id': matched_service.id,

                'name': matched_service.name,

                'description': matched_service.description,

                'price': (
                    str(matched_service.price)
                    if matched_service.price
                    else None
                ),
            },

            'workers': workers_data,

            'query': message
        })

    except Exception as e:

        print(
            "AI Service Assistant Error:",
            e
        )

        return JsonResponse({

            'success': False,

            'message': (
                'Sorry, something went wrong. '
                'Please try again.'
            )
        })

@login_required
def raise_complaint(request, booking_id):

    booking = get_object_or_404(
        Booking,
        id=booking_id,
        customer=request.user
    )

    if booking.status not in ['COMPLETION_REQUESTED', 'COMPLETED']:
        messages.error(request, 'You can raise a complaint only for completed or completion-requested bookings.')
        return redirect('my_bookings')

    if Complaint.objects.filter(booking=booking, customer=request.user).exists():
        messages.warning(request, 'You have already raised a complaint for this booking.')
        return redirect('my_complaints')

    if request.method == 'POST':
        subject = request.POST.get('subject', '').strip()
        description = request.POST.get('description', '').strip()

        if not subject:
            messages.error(request, 'Please enter a complaint subject.')
            return render(request, 'user/raise_complaint.html', {'booking': booking})

        if not description:
            messages.error(request, 'Please describe your complaint.')
            return render(request, 'user/raise_complaint.html', {'booking': booking})

        complaint = Complaint.objects.create(
            booking=booking,
            customer=request.user,
            subject=subject,
            description=description,
            status='OPEN'
        )

        try:
            from .ai_utils import classify_complaint_severity
            severity = classify_complaint_severity(subject, description)
            if severity:
                complaint.severity = severity
                complaint.save(update_fields=['severity'])
        except Exception:
            pass

        Notification.objects.create(
            recipient=request.user,
            booking=booking,
            notification_type='SYSTEM',
            message=f'Your complaint for Booking #{booking.id} has been submitted successfully.'
        )

        messages.success(request, 'Your complaint has been submitted successfully.')
        return redirect('my_complaints')

    return render(request, 'user/raise_complaint.html', {'booking': booking})
