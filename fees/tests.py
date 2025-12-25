# fees/tests.py
from django.test import TestCase
from django.contrib.auth import get_user_model
from core.models import Student, Parent, SchoolClass
from academics.models import Level
from fees.models import FeeType, Fee

User = get_user_model()

class FeeSignalsTest(TestCase):
    def setUp(self):
        # créer level, classe, student
        self.level = Level.objects.create(name="L1")
        self.school_class = SchoolClass.objects.create(name="Class A", level=self.level)
        self.user = User.objects.create_user(username="stu", password="pass")
        self.student = Student.objects.create(user=self.user, school_class=self.school_class)

    def test_fee_created_on_student_create(self):
        # créer fee_type puis nouvel étudiant
        ft = FeeType.objects.create(name="Inscription", level=self.level, default_amount=100.00)
        # nouveau student (setUp a déjà créé un student sans ft présent)
        new_user = User.objects.create_user(username="stu2", password="pass")
        new_student = Student.objects.create(user=new_user, school_class=self.school_class)
        fees = Fee.objects.filter(student=new_student)
        self.assertTrue(fees.exists())
        self.assertEqual(fees.first().amount, ft.default_amount)
