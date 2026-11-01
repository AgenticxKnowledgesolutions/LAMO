from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from lamoapp.models import Notification

User = get_user_model()

class NavbarIntegrationTests(TestCase):
    def setUp(self):
        self.client = Client()
        # Set splash seen cookie so home page renders
        self.client.cookies['lamo_splash_seen'] = '1'
        # Create regular user
        self.user = User.objects.create_user(
            username='testuser',
            email='testuser@example.com',
            password='Password123!',
            role='USER',
            is_active=True
        )

    def test_guest_navbar_has_all_items_and_correct_routes(self):
        """Guest user sees Home, Services, My Bookings, Notifications, Location, Account."""
        response = self.client.get('/?from_splash=1')
        self.assertEqual(response.status_code, 200)

        # Check Home and Services
        self.assertContains(response, reverse('home'))
        self.assertContains(response, reverse('service_page'))

        # Check My Bookings with next parameter for guest
        expected_bookings_login = f"{reverse('login')}?next={reverse('my_bookings')}"
        self.assertContains(response, expected_bookings_login)

        # Check Notifications login for guest
        expected_notif_login = f"{reverse('login')}?next={reverse('notifications')}"
        self.assertContains(response, expected_notif_login)

        # Check Location capsule exists
        self.assertContains(response, 'id="navLocationBtn"')
        self.assertContains(response, 'id="navCurrentDistrict"')

        # Check Account / Login and Register buttons for guest
        self.assertContains(response, reverse('login'))
        self.assertContains(response, reverse('register'))

    def test_authenticated_user_navbar_items_and_unread_count(self):
        """Authenticated user sees actual unread notifications and user dropdown."""
        # Create notifications: 2 unread, 1 read
        Notification.objects.create(
            recipient=self.user,
            message='Unread message 1',
            is_read=False
        )
        Notification.objects.create(
            recipient=self.user,
            message='Unread message 2',
            is_read=False
        )
        Notification.objects.create(
            recipient=self.user,
            message='Read message 3',
            is_read=True
        )

        self.client.login(username='testuser', password='Password123!')
        response = self.client.get('/?from_splash=1')
        self.assertEqual(response.status_code, 200)

        # Check authenticated My Bookings link
        self.assertContains(response, f'href="{reverse("my_bookings")}"')

        # Check actual unread count: 2
        self.assertEqual(response.context['global_unread_count'], 2)
        self.assertContains(response, 'id="lamoNotifBell"')
        self.assertContains(response, '2')  # unread badge number

        # Check User Profile Dropdown
        self.assertContains(response, 'id="userMenuTrigger"')
        self.assertContains(response, 'id="userDropdown"')
        self.assertContains(response, reverse('user_dashboard'))
        self.assertContains(response, reverse('edit_profile'))
        self.assertContains(response, reverse('logout'))

    def test_guest_restricted_navigation_preserves_next_parameter(self):
        """When guest clicks My Bookings or Notifications link, next parameter is accepted by login_view."""
        login_url = f"{reverse('login')}?next={reverse('my_bookings')}"
        response = self.client.get(login_url)
        self.assertEqual(response.status_code, 200)
        # Verify session preserved next
        self.assertEqual(self.client.session.get('registration_next'), reverse('my_bookings'))

    def test_location_selector_displays_selected_district(self):
        """Selecting a district reflects in the navbar location capsule."""
        session = self.client.session
        session['district'] = 'Ernakulam'
        session.save()

        response = self.client.get('/?from_splash=1')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Ernakulam')
