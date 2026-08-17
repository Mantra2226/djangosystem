/**
 * work_order_category_toggle.js
 *
 * Dynamically adjusts Django Admin WorkOrder form field labels, help texts,
 * CSS highlighting, and field visibility based on category selection (PRODUCTION vs PACKAGING).
 */
(function ($) {
    'use strict';

    $(document).ready(function () {
        const $categorySelect = $('#id_category');
        const $productSelect = $('#id_product');
        const $parentRow = $('.field-parent_work_order');
        const $parentLabel = $parentRow.find('label');
        const $qtyRow = $('.field-quantity_produced');
        const $qtyLabel = $qtyRow.find('label');
        let $qtyHelp = $qtyRow.find('.help');

        const $actualQtyRow = $('.field-actual_quantity_produced');
        const $actualQtyLabel = $actualQtyRow.find('label');
        let $actualQtyHelp = $actualQtyRow.find('.help');

        if ($qtyRow.length && !$qtyHelp.length) {
            $qtyHelp = $('<div class="help"></div>').appendTo($qtyRow);
        }
        if ($actualQtyRow.length && !$actualQtyHelp.length) {
            $actualQtyHelp = $('<div class="help"></div>').appendTo($actualQtyRow);
        }

        function togglePackagingFields() {
            let category = $categorySelect.val();

            // If category is unselected, attempt to auto-infer from product text if available
            if (!category && $productSelect.length) {
                const selectedText = $productSelect.find('option:selected').text().toUpperCase();
                if (selectedText.includes('FINISHED') || selectedText.includes('FG-') || selectedText.includes('BOTTLED')) {
                    category = 'PACKAGING';
                } else if (selectedText.includes('INTERMEDIATE') || selectedText.includes('INT-') || selectedText.includes('BULK')) {
                    category = 'PRODUCTION';
                }
            }

            if (category === 'PACKAGING') {
                $parentRow.addClass('highlight-parent-wo').show();
                if ($parentLabel.length) {
                    $parentLabel.html('Source Bulk Batch (Parent WO):');
                }
                if ($qtyLabel.length) {
                    $qtyLabel.html('Target Pack Count (Units/Tins):');
                }
                if ($qtyHelp.length) {
                    $qtyHelp.text('Total discrete containers to fill.');
                }
                if ($actualQtyLabel.length) {
                    $actualQtyLabel.html('Actual Quantity Produced (Units/Tins):');
                }
                if ($actualQtyHelp.length) {
                    $actualQtyHelp.text('Actual count of filled containers produced by operator to save to inventory.');
                }
            } else {
                // PRODUCTION or default
                $parentRow.removeClass('highlight-parent-wo').hide();
                if ($qtyLabel.length) {
                    $qtyLabel.html('Bulk Yield Target (kg/L):');
                }
                if ($qtyHelp.length) {
                    $qtyHelp.text('Total bulk weight/volume to mix.');
                }
                if ($actualQtyLabel.length) {
                    $actualQtyLabel.html('Actual Quantity Produced (kg/L):');
                }
                if ($actualQtyHelp.length) {
                    $actualQtyHelp.text('Actual bulk weight/volume produced by operator to save to inventory.');
                }
            }
        }

        // Listen for change events
        if ($categorySelect.length) {
            $categorySelect.on('change', togglePackagingFields);
        }
        if ($productSelect.length) {
            $productSelect.on('change', togglePackagingFields);
        }

        // Initial execution on page load
        togglePackagingFields();
    });
})(window.django && window.django.jQuery ? window.django.jQuery : (window.jQuery || $));
