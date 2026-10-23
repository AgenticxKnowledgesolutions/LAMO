from django.test import TestCase, Client
from django.urls import reverse
from lamoapp.models import CustomUser, Service, WorkerProfile


class ServiceFilteringTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Create Services
        self.svc_elec = Service.objects.create(
            name='Electrician',
            description='Safe and reliable electrical services, switch repair, wiring.',
            price=250,
            is_active=True,
        )
        self.svc_plumb = Service.objects.create(
            name='Plumbing Services',
            description='Plumbing leaks, pipe fixing, bathroom repair.',
            price=300,
            is_active=True,
        )
        self.svc_clean = Service.objects.create(
            name='Home Cleaning',
            description='Deep home cleaning and sanitization services.',
            price=500,
            is_active=True,
        )
        self.svc_ac = Service.objects.create(
            name='AC Repair & Servicing',
            description='Air conditioner maintenance, gas refill, cooling repair.',
            price=600,
            is_active=True,
        )

        # Create 4 workers for Electrical service (Scenario 3: 4 workers provide Electrical)
        # 3 workers in Kollam, 1 worker in Thiruvananthapuram
        self.workers_elec = []
        for i in range(1, 5):
            u = CustomUser.objects.create_user(
                username=f'electrician_{i}',
                email=f'elec{i}@example.com',
                password='password123',
                role='WORKER',
            )
            dist = 'Kollam' if i <= 3 else 'Thiruvananthapuram'
            w = WorkerProfile.objects.create(
                user=u,
                service=self.svc_elec,
                district=dist,
                is_verified=True,
                work_status='AVAILABLE',
                pricing_amount=250 + i * 10,
            )
            self.workers_elec.append(w)

        # Create Plumber in Kollam
        u_plumb = CustomUser.objects.create_user(
            username='plumber_kollam',
            email='plumb@example.com',
            password='password123',
            role='WORKER',
        )
        self.w_plumb = WorkerProfile.objects.create(
            user=u_plumb,
            service=self.svc_plumb,
            district='Kollam',
            is_verified=True,
            work_status='AVAILABLE',
            pricing_amount=300,
        )

        # Create Cleaner in Thiruvananthapuram only
        u_clean = CustomUser.objects.create_user(
            username='cleaner_tvm',
            email='clean@example.com',
            password='password123',
            role='WORKER',
        )
        self.w_clean = WorkerProfile.objects.create(
            user=u_clean,
            service=self.svc_clean,
            district='Thiruvananthapuram',
            is_verified=True,
            work_status='AVAILABLE',
            pricing_amount=500,
        )

        # Session setup so splash screen does not intercept
        s = self.client.session
        s['lamo_splash_shown'] = True
        s.save()
        self.client.cookies['lamo_splash_seen'] = '1'

    def test_scenario_1_click_electrical_category(self):
        """Scenario 1: Click Electrical -> only relevant electrical services appear."""
        resp = self.client.get(reverse('home'), {'category': 'electrician', 'ajax': '1'})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['status'], 'success')
        service_names = [s['name'] for s in data['services']]
        self.assertIn('Electrician', service_names)
        self.assertNotIn('Plumbing Services', service_names)
        self.assertNotIn('Home Cleaning', service_names)

        # Also test with category='electrical' alias
        resp_alias = self.client.get(reverse('home'), {'category': 'electrical', 'ajax': '1'})
        data_alias = resp_alias.json()
        alias_names = [s['name'] for s in data_alias['services']]
        self.assertIn('Electrician', alias_names)
        self.assertNotIn('Plumbing Services', alias_names)

    def test_scenario_2_search_suggestion_electrician(self):
        """Scenario 2: Click the Electrician popular search suggestion -> relevant electrical services appear."""
        resp = self.client.get(reverse('home'), {'search': 'Electrician', 'ajax': '1'})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['status'], 'success')
        service_names = [s['name'] for s in data['services']]
        self.assertEqual(service_names, ['Electrician'])

    def test_scenario_3_four_workers_single_service_card(self):
        """Scenario 3: If four workers provide Electrical services, display the service only ONCE."""
        # Across all districts or in Kollam where 3 workers offer it
        resp = self.client.get(reverse('home'), {'district': 'Kollam', 'search': 'Electrician', 'ajax': '1'})
        data = resp.json()
        self.assertEqual(data['services_count'], 1)
        self.assertEqual(len(data['services']), 1)
        self.assertEqual(data['services'][0]['id'], self.svc_elec.id)
        self.assertEqual(data['services'][0]['worker_count'], 3)

        # Check HTML render as well (SSR)
        resp_html = self.client.get(reverse('home'), {'district': 'Kollam', 'search': 'Electrician'})
        self.assertEqual(resp_html.status_code, 200)
        services = resp_html.context['services']
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0].id, self.svc_elec.id)

    def test_scenario_4_click_all_services(self):
        """Scenario 4: Click All Services -> all eligible services appear without duplicates."""
        resp = self.client.get(reverse('home'), {'district': 'Kollam', 'category': 'all', 'ajax': '1'})
        data = resp.json()
        self.assertEqual(data['status'], 'success')
        service_ids = [s['id'] for s in data['services']]
        # Must have no duplicates
        self.assertEqual(len(service_ids), len(set(service_ids)))
        # In Kollam, Electrician (3 workers) and Plumbing (1 worker) have active workers
        self.assertIn(self.svc_elec.id, service_ids)
        self.assertIn(self.svc_plumb.id, service_ids)
        # Home cleaning is in TVM only, so not in Kollam
        self.assertNotIn(self.svc_clean.id, service_ids)

    def test_scenario_5_select_kollam(self):
        """Scenario 5: Select Kollam -> only services available in Kollam appear."""
        resp = self.client.get(reverse('home'), {'district': 'Kollam', 'ajax': '1'})
        data = resp.json()
        service_names = [s['name'] for s in data['services']]
        self.assertIn('Electrician', service_names)
        self.assertIn('Plumbing Services', service_names)
        self.assertNotIn('Home Cleaning', service_names)

    def test_scenario_6_select_another_district(self):
        """Scenario 6: Select another district (Thiruvananthapuram) -> results update correctly."""
        resp = self.client.get(reverse('home'), {'district': 'Thiruvananthapuram', 'ajax': '1'})
        data = resp.json()
        service_names = [s['name'] for s in data['services']]
        # TVM has Electrician and Home Cleaning
        self.assertIn('Electrician', service_names)
        self.assertIn('Home Cleaning', service_names)
        # Plumbing is only in Kollam
        self.assertNotIn('Plumbing Services', service_names)

        # Select a district with no workers (e.g. Wayanad)
        resp_empty = self.client.get(reverse('home'), {'district': 'Wayanad', 'ajax': '1'})
        data_empty = resp_empty.json()
        self.assertEqual(data_empty['services_count'], 0)
        self.assertEqual(len(data_empty['services']), 0)

    def test_scenario_7_combine_location_category_search(self):
        """Scenario 7: Combine a location selection with a category and search query -> all three work together."""
        # District: Kollam, Category: electrician, Search: Electrician -> matches
        resp = self.client.get(reverse('home'), {
            'district': 'Kollam',
            'category': 'electrician',
            'search': 'Electrician',
            'ajax': '1'
        })
        data = resp.json()
        self.assertEqual(data['services_count'], 1)
        self.assertEqual(data['services'][0]['name'], 'Electrician')

        # District: Kollam, Category: plumb, Search: Electrician -> conflict, 0 results
        resp_conflict = self.client.get(reverse('home'), {
            'district': 'Kollam',
            'category': 'plumb',
            'search': 'Electrician',
            'ajax': '1'
        })
        data_conflict = resp_conflict.json()
        self.assertEqual(data_conflict['services_count'], 0)

    def test_scenario_8_service_card_navigation_url(self):
        """Scenario 8: Click a service card -> navigate to the correct service details or worker listing page."""
        # When district is selected, URL points to service_workers with district
        resp_dist = self.client.get(reverse('home'), {'district': 'Kollam', 'ajax': '1'})
        data_dist = resp_dist.json()
        elec_service = next(s for s in data_dist['services'] if s['id'] == self.svc_elec.id)
        self.assertEqual(elec_service['url'], f'/services/{self.svc_elec.id}/workers/?district=Kollam')

        # When no district is selected, URL points to service_detail
        s = self.client.session
        s.pop('district', None)
        s.pop('lamo_selected_district', None)
        s.save()
        resp_nodist = self.client.get(reverse('home'), {'district': '', 'change_district': '1', 'ajax': '1'})
        data_nodist = resp_nodist.json()
        elec_nodist = next(s for s in data_nodist['services'] if s['id'] == self.svc_elec.id)
        self.assertEqual(elec_nodist['url'], f'/services/{self.svc_elec.id}/')

    def test_scenario_9_clear_search_restores_remaining_filters(self):
        """Scenario 9: Clear the search -> restore the appropriate results based on the remaining filters."""
        # First search with query that narrows down
        resp_searched = self.client.get(reverse('home'), {
            'district': 'Kollam',
            'search': 'Electrician',
            'ajax': '1'
        })
        self.assertEqual(resp_searched.json()['services_count'], 1)

        # Clear search (search=''), district remains Kollam
        resp_cleared = self.client.get(reverse('home'), {
            'district': 'Kollam',
            'search': '',
            'ajax': '1'
        })
        data_cleared = resp_cleared.json()
        # In Kollam: Electrician and Plumbing Services are restored
        service_names = [s['name'] for s in data_cleared['services']]
        self.assertIn('Electrician', service_names)
        self.assertIn('Plumbing Services', service_names)
        self.assertEqual(data_cleared['services_count'], 2)
