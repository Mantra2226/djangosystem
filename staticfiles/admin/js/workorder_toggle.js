/**
 * workorder_toggle.js
 *
 * Dynamic Field Toggling for Django Admin WorkOrder change form.
 * Listens to changes on the category field:
 * - If category is PRODUCTION (Bulk Mixing): hides .field-parent_work_order
 * - If category is PACKAGING: displays .field-parent_work_order
 */
(function ($) {
    'use strict';

    $(document).ready(function () {
        const $categorySelect = $('#id_category');
        const $productSelect = $('#id_product');
        const $parentRow = $('.field-parent_work_order');

        function toggleWorkOrderFields() {
            // Defensive guard: bail out if critical DOM elements are absent
            if (!$categorySelect.length && !$parentRow.length) {
                return;
            }

            let category = $categorySelect.val();

            // If category is unselected, attempt to infer from product option text
            if (!category && $productSelect.length) {
                const selectedText = ($productSelect.find('option:selected').text() || '').toUpperCase();
                if (selectedText.includes('FINISHED') || selectedText.includes('PACKAGING') || selectedText.includes('TIN')) {
                    category = 'PACKAGING';
                } else if (selectedText.includes('INTERMEDIATE') || selectedText.includes('BULK') || selectedText.includes('RAW')) {
                    category = 'PRODUCTION';
                }
            }

            if (category === 'PACKAGING') {
                $parentRow.show();
            } else if (category === 'PRODUCTION' || category === 'BULK_PRODUCTION') {
                $parentRow.hide();
            } else {
                // Default: show if parent is already set, else hide
                const parentVal = $('#id_parent_work_order').val();
                if (parentVal) {
                    $parentRow.show();
                } else {
                    $parentRow.hide();
                }
            }
        }

        if ($categorySelect.length) {
            $categorySelect.on('change', toggleWorkOrderFields);
        }
        if ($productSelect.length) {
            $productSelect.on('change', toggleWorkOrderFields);
        }

        // Run on initial page load
        toggleWorkOrderFields();
    });
})(window.django && window.django.jQuery ? window.django.jQuery : (window.jQuery || $));
