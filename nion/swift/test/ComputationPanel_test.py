# futures
from __future__ import absolute_import

# standard libraries
import logging
import unittest

# third party libraries
import numpy

# local libraries
from nion.swift import Application
from nion.swift import DocumentController
from nion.swift.model import DataItem
from nion.swift.model import DocumentModel
from nion.swift.model import Symbolic
from nion.ui import Test


class TestComputationPanelClass(unittest.TestCase):

    def setUp(self):
        self.app = Application.Application(Test.UserInterface(), set_global=False)

    def tearDown(self):
        pass

    def test_expression_updates_when_node_is_changed(self):
        document_model = DocumentModel.DocumentModel()
        document_controller = DocumentController.DocumentController(self.app.ui, document_model, workspace_id="library")
        panel = document_controller.find_dock_widget("computation-panel").panel
        data_item1 = DataItem.DataItem(numpy.zeros((10, 10)))
        document_model.append_data_item(data_item1)
        data_item2 = DataItem.DataItem(numpy.zeros((10, 10)))
        document_model.append_data_item(data_item2)
        map = {"a": document_model.get_object_specifier(data_item1)}
        computation = Symbolic.Computation()
        computation.parse_expression(document_model, "-a", map)
        data_item2.maybe_data_source.set_computation(computation)
        document_controller.display_data_item(DataItem.DisplaySpecifier.from_data_item(data_item2))
        document_controller.periodic()  # execute queue
        text1 = panel._text_edit_for_testing.text
        self.assertEqual(text1, computation.reconstruct(document_controller.build_variable_map()))
        data_item2.maybe_data_source.computation.parse_expression(document_model, "-a+1", map)
        document_controller.periodic()  # execute queue
        text2 = panel._text_edit_for_testing.text
        self.assertEqual(text2, computation.reconstruct(document_controller.build_variable_map()))
        self.assertNotEqual(text2, text1)

    def test_clearing_computation_clears_text_and_unbinds_or_whatever(self):
        document_model = DocumentModel.DocumentModel()
        document_controller = DocumentController.DocumentController(self.app.ui, document_model, workspace_id="library")
        panel = document_controller.find_dock_widget("computation-panel").panel
        data_item1 = DataItem.DataItem(numpy.zeros((10, 10)))
        document_model.append_data_item(data_item1)
        data_item2 = DataItem.DataItem(numpy.zeros((10, 10)))
        document_model.append_data_item(data_item2)
        map = {"a": document_model.get_object_specifier(data_item1)}
        computation = Symbolic.Computation()
        computation.parse_expression(document_model, "-a", map)
        data_item2.maybe_data_source.set_computation(computation)
        document_controller.display_data_item(DataItem.DisplaySpecifier.from_data_item(data_item2))
        document_controller.periodic()  # execute queue
        text1 = panel._text_edit_for_testing.text
        self.assertEqual(text1, computation.reconstruct(document_controller.build_variable_map()))
        panel._text_edit_for_testing.text = ""
        panel._text_edit_for_testing.on_return_pressed()
        document_controller.periodic()  # execute queue
        self.assertIsNone(data_item2.maybe_data_source.computation)
        text2 = panel._text_edit_for_testing.text
        self.assertIsNone(text2)

    def test_invalid_expression_shows_error_and_clears_it(self):
        document_model = DocumentModel.DocumentModel()
        document_controller = DocumentController.DocumentController(self.app.ui, document_model, workspace_id="library")
        panel = document_controller.find_dock_widget("computation-panel").panel
        data_item1 = DataItem.DataItem(numpy.zeros((10, 10)))
        document_model.append_data_item(data_item1)
        data_item2 = DataItem.DataItem(numpy.zeros((10, 10)))
        document_model.append_data_item(data_item2)
        map = {"a": document_model.get_object_specifier(data_item1)}
        computation = Symbolic.Computation()
        computation.parse_expression(document_model, "-a", map)
        data_item2.maybe_data_source.set_computation(computation)
        document_controller.display_data_item(DataItem.DisplaySpecifier.from_data_item(data_item2))
        document_controller.periodic()  # execute queue
        expression = panel._text_edit_for_testing.text
        self.assertIsNone(panel._error_label_for_testing.text)
        panel._text_edit_for_testing.text = "xyz(a)"
        panel._text_edit_for_testing.on_return_pressed()
        self.assertEqual(panel._text_edit_for_testing.text, "xyz(a)")
        self.assertTrue(len(panel._error_label_for_testing.text) > 0)
        panel._text_edit_for_testing.text = expression
        panel._text_edit_for_testing.on_return_pressed()
        self.assertEqual(panel._text_edit_for_testing.text, expression)
        self.assertIsNone(panel._error_label_for_testing.text)

    def test_variables_get_updates_when_switching_data_items(self):
        document_model = DocumentModel.DocumentModel()
        document_controller = DocumentController.DocumentController(self.app.ui, document_model, workspace_id="library")
        panel = document_controller.find_dock_widget("computation-panel").panel
        data_item1 = DataItem.DataItem(numpy.zeros((10, 10)))
        document_model.append_data_item(data_item1)
        data_item2 = DataItem.DataItem(numpy.zeros((10, 10)))
        document_model.append_data_item(data_item2)
        computation = Symbolic.Computation()
        computation.create_object("a", document_model.get_object_specifier(data_item1))
        computation.create_variable("x", value_type="integral", value=5)
        computation.parse_expression(document_model, "a + x", dict())
        data_item2.maybe_data_source.set_computation(computation)
        document_controller.display_data_item(DataItem.DisplaySpecifier.from_data_item(data_item1))
        document_controller.periodic()  # execute queue
        self.assertEqual(len(panel._sections_for_testing), 0)
        document_controller.display_data_item(DataItem.DisplaySpecifier.from_data_item(data_item2))
        document_controller.periodic()  # execute queue
        self.assertEqual(len(panel._sections_for_testing), 2)
        document_controller.display_data_item(DataItem.DisplaySpecifier.from_data_item(data_item1))
        document_controller.periodic()  # execute queue
        self.assertEqual(len(panel._sections_for_testing), 0)

    def disabled_test_expression_updates_when_variable_is_assigned(self):
        raise Exception()

    def disabled_test_computation_panel_provides_help_button(self):
        raise Exception()

    def disabled_test_new_button_create_new_data_item(self):
        raise Exception()

    def disabled_test_invalid_expression_saves_text_for_editing(self):
        raise Exception()

    def disabled_test_knobs_for_computations_appear_in_inspector(self):
        assert False