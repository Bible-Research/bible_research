from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

User = get_user_model()


class BiblePassageViewValidationTests(TestCase):
    """Input-validation error contract for ``BiblePassageView``.

    Regression tests for GitHub issue #57: the missing-``passage``
    400 used to document ``?passage=John+3:16``; verse selectors
    are now supported (issue #19), but the documented example
    stays chapter-only. Non-numeric or malformed chapter parts
    fail ``_parse_chapter_part``'s format check before reaching
    the positive-number guard.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username='passagetest',
            email='passage@test.local',
            password='x',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.url = reverse('bible-passage')

    def test_missing_passage_example_is_a_supported_format(self):
        resp = self.client.get(self.url, {'fileset_id': 'LVSGLU8'})
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body['error_code'], 'missing_parameter')
        error = body['error']
        self.assertIn('Passage parameter is required', error)
        # The documented example must be a passage the endpoint
        # actually accepts (book + chapter only).
        self.assertIn('?passage=John+3', error)
        self.assertNotIn('3:16', error)

    def test_invalid_fileset_id_rejected_before_routing(self):
        """Garbage fileset ids 400 at the view boundary — before
        they can reach the DBT branch or an upstream URL path."""
        resp = self.client.get(
            self.url,
            {
                'passage': 'John 3',
                'fileset_id': 'evil/../x?key=',
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json()['error_code'], 'invalid_parameter'
        )

    def test_invalid_format_400_carries_error_code(self):
        resp = self.client.get(
            self.url,
            {
                'passage': 'John 3',
                'fileset_id': 'LVSGLU8',
                'response_format': 'video',
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json()['error_code'], 'invalid_parameter'
        )

    def test_non_positive_chapter_reports_positive_number(self):
        resp = self.client.get(
            self.url,
            {'passage': 'John 0', 'fileset_id': 'LVSGLU8'},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json()['error'],
            'Chapter must be a positive number',
        )

    def test_malformed_chapter_reports_passage_format(self):
        # Signed and non-numeric chapter parts do not match the
        # chapter[:verses] grammar, so they surface the generic
        # passage-format error instead of the chapter messages.
        for passage in ('John -2', 'John three'):
            with self.subTest(passage=passage):
                resp = self.client.get(
                    self.url,
                    {'passage': passage, 'fileset_id': 'LVSGLU8'},
                )
                self.assertEqual(resp.status_code, 400)
                self.assertIn(
                    'Invalid passage format',
                    resp.json()['error'],
                )
