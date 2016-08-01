"""Data models describing reports and email notifications

Includes Reports which may or may not have been sent as well as records of when
reports of isolated or related incidents
"""

import hashlib

import nacl.secret
import nacl.utils
import six
from nacl.exceptions import CryptoError
from polymorphic.models import PolymorphicModel

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.mail.message import EmailMultiAlternatives
from django.db import models
from django.template import Context, Template
from django.utils import timezone
from django.utils.crypto import get_random_string, pbkdf2
from django.utils.html import strip_tags


def _encrypt_report(salt, key, report_text):
    """Encrypts a report using the given secret key & salt. The secret key is stretched to 32 bytes using Django's
    PBKDF2+SHA256 implementation. The encryption uses PyNacl & Salsa20 stream cipher.

    Args:
      salt (str): cryptographic salt
      key (str): secret key
      report_text (str): full report as a string

    Returns:
      bytes: the encrypted bytes of the report

    """
    stretched_key = pbkdf2(key, salt, settings.KEY_ITERATIONS, digest=hashlib.sha256)
    box = nacl.secret.SecretBox(stretched_key)
    message = report_text.encode('utf-8')
    nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)
    return box.encrypt(message, nonce)


def _decrypt_report(salt, key, encrypted):
    """Decrypts an encrypted report.

    Args:
      salt (str): cryptographic salt
      key (str): secret key
      encrypted (bytes): full report encrypted

    Returns:
      str: the decrypted report as a string

    Raises:
      CryptoError: If the key and salt fail to decrypt the record.

    """
    stretched_key = pbkdf2(key, salt, settings.KEY_ITERATIONS, digest=hashlib.sha256)
    box = nacl.secret.SecretBox(stretched_key)
    decrypted = box.decrypt(bytes(encrypted))  # need to force to bytes bc BinaryField can return as memoryview
    return decrypted.decode('utf-8')


def _pepper(encrypted_report):
    """Uses a secret value stored on the server to encrypt an already encrypted report, to add protection if the database
    is breached but the server is not. Requires settings.PEPPER to be set to a 32 byte value. In production, this value
    should be set via environment parameter. Uses PyNacl's Salsa20 stream cipher.

    Args:
      encrypted_report (bytes): the encrypted report

    Returns:
      bytes: a further encrypted report

    """
    pepper = settings.PEPPER
    box = nacl.secret.SecretBox(pepper)
    nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)
    return box.encrypt(encrypted_report, nonce)


def _unpepper(peppered_report):
    """Decrypts a report that has been peppered with the _pepper method. Requires settings.PEPPER to be set to a 32
    byte value. In production, this value should be set via environment parameter.

    Args:
      peppered_report(bytes): a report that has been encrypted using a secret key then encrypted using the pepper

    Returns:
      bytes: the report, still encrypted with the secret key

    Raises:
      CryptoError: If the pepper fails to decrypt the record.
    """
    pepper = settings.PEPPER
    box = nacl.secret.SecretBox(pepper)
    decrypted = box.decrypt(bytes(peppered_report))  # need to force to bytes bc BinaryField can return as memoryview
    return decrypted


class Report(models.Model):
    """The full text of a reported incident."""
    encrypted = models.BinaryField(blank=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL)
    added = models.DateTimeField(auto_now_add=True)
    autosaved = models.BooleanField(null=False, default=False)
    last_edited = models.DateTimeField(blank=True, null=True)
    salt = models.CharField(blank=False, max_length=256)

    submitted_to_school = models.DateTimeField(blank=True, null=True)
    contact_phone = models.CharField(blank=True, null=True, max_length=256)
    contact_voicemail = models.TextField(blank=True, null=True)
    contact_email = models.EmailField(blank=True, null=True, max_length=256)
    contact_notes = models.TextField(blank=True, null=True)
    contact_name = models.TextField(blank=True, null=True)

    @property
    def entered_into_matching(self):
        first_match_report = self.matchreport_set.first()
        if first_match_report:
            return first_match_report.added
        else:
            return None

    match_found = models.BooleanField(default=False)

    class Meta:
        ordering = ('-added',)

    def encrypt_report(self, report_text, key, edit=False, autosave=False):
        """Encrypts and attaches report text. Generates a random salt and stores it on the Report object.

        Args:
          report_text (str): the full text of the report
          key (str): the secret key
          edit (obj): the object to edit
          autosave (bool): whether or not this encryption is part of an automatic save

        """
        if not self.salt:
            self.salt = get_random_string()
        elif edit:
            self.last_edited = timezone.now()
        self.autosaved = autosave
        self.encrypted = _encrypt_report(salt=self.salt, key=key, report_text=report_text)

    def decrypted_report(self, key):
        """Decrypts the report text. Uses the salt stored on the Report object.
        Args:
          key (str): the secret key

        Returns:
          str: the decrypted report as a string

        Raises:
          CryptoError: If the key and saved salt fail to decrypt the record.
        """
        return _decrypt_report(salt=self.salt, key=key, encrypted=self.encrypted)

    def withdraw_from_matching(self):
        """ Deletes all associated MatchReports """
        self.matchreport_set.all().delete()
        self.match_found = False

    @property
    def get_submitted_report_id(self):
        """Return the ID of the first time a FullReport was submitted."""
        if self.submitted_to_school:
            sent_report = self.sentfullreport_set.first()
            report_id = sent_report.get_report_id() if sent_report else None
            return report_id
        else:
            return None


@six.python_2_unicode_compatible
class EmailNotification(models.Model):
    """Record of Email constructed in and sent via the project"""
    name = models.CharField(blank=False, max_length=50, primary_key=True)
    subject = models.CharField(blank=False, max_length=77)
    body = models.TextField(blank=False)

    def __str__(self):
        return self.name

    def render_body(self, context=None):
        """Format the email as HTML."""
        if context is None:
            context = {}
        current_site = Site.objects.get_current()
        context['domain'] = current_site.domain
        return Template(self.body).render(Context(context))

    def render_body_plain(self, context=None):
        """Format the email as plain text."""
        if context is None:
            context = {}
        html = self.render_body(context)
        cleaned = html.replace('<br />', '\n')
        cleaned = cleaned.replace('<br/>', '\n')
        cleaned = cleaned.replace('<p>', '\n')
        cleaned = cleaned.replace('</p>', '\n')
        return strip_tags(cleaned)

    def send(self, to, from_email, context=None):
        """Send the email as plain text.

        Includes an HTML equivalent version as an attachment.
        """

        if context is None:
            context = {}
        email = EmailMultiAlternatives(self.subject, self.render_body_plain(context), from_email, to)
        email.attach_alternative(self.render_body(context), "text/html")
        email.send()


@six.python_2_unicode_compatible
class MatchReport(models.Model):
    """A report that indicates the user wants to submit if a match is found. A single report can have multiple
    MatchReports--one per perpetrator.
    """
    report = models.ForeignKey('Report')
    contact_email = models.EmailField(blank=False, max_length=256)

    identifier = models.CharField(blank=False, null=True, max_length=500)

    added = models.DateTimeField(auto_now_add=True)
    seen = models.BooleanField(blank=False, default=False)

    encrypted = models.BinaryField(null=False)
    salt = models.CharField(null=False, max_length=256)

    def __str__(self):
        return "Match report for report {0}".format(self.report.pk)

    def encrypt_match_report(self, report_text, key):
        """Encrypts and attaches report text. Generates a random salt and stores it on the MatchReport object.

        Args:
          report_text (str): the full text of the report
          key (str): the secret key

        """
        self.salt = get_random_string()
        self.encrypted = _pepper(_encrypt_report(salt=self.salt, key=key, report_text=report_text))

    def get_match(self, identifier):
        """Checks if the given identifier triggers a match on this report. Returns report text if so.

        Args:
          identifier (str): the identifier provided by the user when entering matching.

        Returns:
            str or None: returns the decrypted report as a string if the identifier matches, or None otherwise.
        """
        decrypted_report = None
        try:
            decrypted_report = _decrypt_report(salt=self.salt, key=identifier, encrypted=_unpepper(self.encrypted))
        except CryptoError:
            pass
        return decrypted_report


class SentReport(PolymorphicModel):
    """Report of one or more incidents, sent to the monitoring organization"""
    # TODO: store link to s3 backup https://github.com/SexualHealthInnovations/callisto-core/issues/14
    sent = models.DateTimeField(auto_now_add=True)
    to_address = models.EmailField(blank=False, null=False, max_length=256)

    def _get_id_for_schools(self, is_match):
        return "{0}-{1}-{2}".format(settings.SCHOOL_REPORT_PREFIX, '%05d' % self.id, 0 if is_match else 1)


class SentFullReport(SentReport):
    """Report of a single incident since to the monitoring organization"""
    report = models.ForeignKey(Report, blank=True, null=True, on_delete=models.SET_NULL)

    def get_report_id(self):
        return self._get_id_for_schools(is_match=False)


class SentMatchReport(SentReport):
    """Report of multiple incidents, sent to the monitoring organization"""
    reports = models.ManyToManyField(MatchReport)

    def get_report_id(self):
        return self._get_id_for_schools(is_match=True)
