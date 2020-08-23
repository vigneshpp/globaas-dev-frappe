# -*- coding: utf-8 -*-
# Copyright (c) 2019, Frappe Technologies and contributors
# For license information, please see license.txt

from __future__ import unicode_literals

import frappe
from frappe.model.document import Document
from frappe.desk.form import assign_to
import frappe.cache_manager
from frappe import _

class AssignmentRule(Document):

	def validate(self):
		assignment_days = self.get_assignment_days()
		if not len(set(assignment_days)) == len(assignment_days):
			repeated_days = get_repeated(assignment_days)
			frappe.throw(_("Assignment Day {0} has been repeated.".format(frappe.bold(repeated_days))))

	def on_update(self): # pylint: disable=no-self-use
		frappe.cache_manager.clear_doctype_map('Assignment Rule', self.name)

	def after_rename(self): # pylint: disable=no-self-use
		frappe.cache_manager.clear_doctype_map('Assignment Rule', self.name)

	def apply_unassign(self, doc, assignments):
		if (self.unassign_condition and
			self.name in [d.assignment_rule for d in assignments]):
			return self.clear_assignment(doc)

		return False

	def apply_close(self, doc, assignments):
		if (self.close_assignments and
			self.name in [d.assignment_rule for d in assignments]):
			return self.close_assignments(doc)

		return False

	def apply_assign(self, doc):
		if self.safe_eval('assign_condition', doc):
			self.do_assignment(doc)
			return True

	def do_assignment(self, doc):
		# clear existing assignment, to reassign
		assign_to.clear(doc.get('doctype'), doc.get('name'))

		user = self.get_user()

		assign_to.add(dict(
			assign_to = user,
			doctype = doc.get('doctype'),
			name = doc.get('name'),
			description = frappe.render_template(self.description, doc),
			assignment_rule = self.name,
			notify = True
		))

		# set for reference in round robin
		self.db_set('last_user', user)

	def clear_assignment(self, doc):
		'''Clear assignments'''
		if self.safe_eval('unassign_condition', doc):
			return assign_to.clear(doc.get('doctype'), doc.get('name'))

	def close_assignments(self, doc):
		'''Close assignments'''
		if self.safe_eval('close_condition', doc):
			return assign_to.close_all_assignments(doc.get('doctype'), doc.get('name'))

	def get_user(self):
		'''
		Get the next user for assignment
		'''
		if self.rule == 'Round Robin':
			return self.get_user_round_robin()
		elif self.rule == 'Load Balancing':
			return self.get_user_load_balancing()

	def get_user_round_robin(self):
		'''
		Get next user based on round robin
		'''

		# first time, or last in list, pick the first
		if not self.last_user or self.last_user == self.users[-1].user:
			return self.users[0].user

		# find out the next user in the list
		for i, d in enumerate(self.users):
			if self.last_user == d.user:
				return self.users[i+1].user

		# bad last user, assign to the first one
		return self.users[0].user

	def get_user_load_balancing(self):
		'''Assign to the user with least number of open assignments'''
		counts = []
		for d in self.users:
			counts.append(dict(
				user = d.user,
				count = frappe.db.count('ToDo', dict(
					reference_type = self.document_type,
					owner = d.user,
					status = "Open"))
			))

		# sort by dict value
		sorted_counts = sorted(counts, key = lambda k: k['count'])

		# pick the first user
		return sorted_counts[0].get('user')

	def safe_eval(self, fieldname, doc):
		try:
			if self.get(fieldname):
				return frappe.safe_eval(self.get(fieldname), None, doc)
		except Exception as e:
			# when assignment fails, don't block the document as it may be
			# a part of the email pulling
			frappe.msgprint(frappe._('Auto assignment failed: {0}').format(str(e)), indicator = 'orange')

		return False

	def get_assignment_days(self):
		return [d.day for d in self.get('assignment_days', [])]

	def is_rule_not_applicable_today(self):
		today = frappe.flags.assignment_day or frappe.utils.get_weekday()
		assignment_days = self.get_assignment_days()
		if assignment_days and not today in assignment_days:
			return True

		return False

def get_assignments(doc):
	return frappe.get_all('ToDo', fields = ['name', 'assignment_rule'], filters = dict(
		reference_type = doc.get('doctype'),
		reference_name = doc.get('name'),
		status = ('!=', 'Cancelled')
	), limit = 5)

@frappe.whitelist()
def bulk_apply(doctype, docnames):
	import json
	docnames = json.loads(docnames)

	background = len(docnames) > 5
	for name in docnames:
		if background:
			frappe.enqueue('frappe.automation.doctype.assignment_rule.assignment_rule.apply', doc=None, doctype=doctype, name=name)
		else:
			apply(None, doctype=doctype, name=name)

def reopen_closed_assignment(doc):
	todo = frappe.db.exists('ToDo', dict(
		reference_type = doc.doctype,
		reference_name = doc.name,
		status = 'Closed'
	))
	if not todo:
		return False
	todo = frappe.get_doc("ToDo", todo)
	todo.status = 'Open'
	todo.save(ignore_permissions=True)
	return True

def apply(doc, method=None, doctype=None, name=None):
	if frappe.flags.in_patch or frappe.flags.in_install or frappe.flags.in_setup_wizard:
		return

	if not doc and doctype and name:
		doc = frappe.get_doc(doctype, name)

	assignment_rules = frappe.cache_manager.get_doctype_map('Assignment Rule', doc.doctype, dict(
		document_type = doc.doctype, disabled = 0), order_by = 'priority desc')

	assignment_rule_docs = []

	# multiple auto assigns
	for d in assignment_rules:
		assignment_rule_docs.append(frappe.get_doc('Assignment Rule', d.get('name')))

	if not assignment_rule_docs:
		return

	doc = doc.as_dict()
	assignments = get_assignments(doc)

	clear = True # are all assignments cleared
	new_apply = False # are new assignments applied

	if assignments:
		# first unassign
		# use case, there are separate groups to be assigned for say L1 and L2,
		# so when the value switches from L1 to L2, L1 team must be unassigned, then L2 can be assigned.
		clear = False
		for assignment_rule in assignment_rule_docs:
			if assignment_rule.is_rule_not_applicable_today():
				continue

			clear = assignment_rule.apply_unassign(doc, assignments)
			if clear:
				break

	# apply rule only if there are no existing assignments
	if clear:
		for assignment_rule in assignment_rule_docs:
			if assignment_rule.is_rule_not_applicable_today():
				continue

			new_apply = assignment_rule.apply_assign(doc)
			if new_apply:
				break

	# apply close rule only if assignments exists
	assignments = get_assignments(doc)
	if assignments:
		for assignment_rule in assignment_rule_docs:
			if assignment_rule.is_rule_not_applicable_today():
				continue

			if not new_apply:
				if not assignment_rule.safe_eval('close_condition', doc):
					reopen = reopen_closed_assignment(doc)
					if reopen:
						break
			close = assignment_rule.apply_close(doc, assignments)
			if close:
				break


def get_assignment_rules():
	return [d.document_type for d in frappe.db.get_all('Assignment Rule', fields=['document_type'], filters=dict(disabled = 0))]

def get_repeated(values):
	unique_list = []
	diff = []
	for value in values:
		if value not in unique_list:
			unique_list.append(str(value))
		else:
			if value not in diff:
				diff.append(str(value))
	return " ".join(diff)
