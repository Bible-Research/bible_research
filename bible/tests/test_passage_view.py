from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

User = get_user_model()


class BiblePassageViewValidationTests(TestCase):
    """Input-validation error contract for ``BiblePassageView``.

    Regression tests for GitHub issue #57: the missing-``passage``
    400 used to document ``?passage=John+3:16`` even though verse
    selectors are rejected by the endpoint, and the "positive
    number" chapter error was dead code swallowed by the
    ``int()`` ``except ValueError`` handler.
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
        error = resp.json()['error']
        self.assertIn('Passage parameter is required', error)
        # The documented example must be a passage the endpoint
        # actually accepts (book + chapter only).
        self.assertIn('?passage=John+3', error)
        self.assertNotIn('3:16', error)

    def test_verse_selector_is_rejected_as_invalid_number(self):
        resp = self.client.get(
            self.url,
            {'passage': 'John 3:16', 'fileset_id': 'LVSGLU8'},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json()['error'], 'Chapter must be a valid number'
        )

    def test_non_positive_chapter_reports_positive_number(self):
        for passage in ('John 0', 'John -2'):
            with self.subTest(passage=passage):
                resp = self.client.get(
                    self.url,
                    {'passage': passage, 'fileset_id': 'LVSGLU8'},
                )
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(
                    resp.json()['error'],
                    'Chapter must be a positive number',
                )

    def test_non_numeric_chapter_reports_valid_number(self):
        resp = self.client.get(
            self.url,
            {'passage': 'John three', 'fileset_id': 'LVSGLU8'},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json()['error'], 'Chapter must be a valid number'
        )
